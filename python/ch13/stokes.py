#!/usr/bin/env python

import sys
from argparse import ArgumentParser, RawTextHelpFormatter
from firedrake import *
from firedrake.petsc import PETSc

parser = ArgumentParser(description="""
Solve a linear Stokes problem in 2D.  Three problem cases:
  1. Lid-driven cavity with quadratic velocity on lid and Dirichlet conditions
     on all sides and a null space of constant pressures.  The default problem.
  2. (-analytical) Analytical exact solution from Logg et al (2012).
  3. (-nobase) Same as 1. but with stress-free condition on bottom so the null
     space is trivial.
Uses mixed FE method, either Taylor-Hood family (P^k x P^l, or Q^k x Q^l with
-quad) or CD with discontinuous pressure; defaults to P^2 x P^1.  Uses either
uniform mesh or reads a mesh in Gmsh format.  Serves as an example of a
saddle-point system.  See the code for Schur+GMG based PC packages:
  -schurgmg diag|diag_mass|lower|lower_mass
The prefix for PETSC solver options is 's_'.""",formatter_class=RawTextHelpFormatter)

parser.add_argument('-analytical', action='store_true', default=False,
                    help='use problem with exact solution')
parser.add_argument('-dp', action='store_true', default=False,
                    help='use discontinuous-Galerkin finite elements for pressure')
parser.add_argument('-i', metavar='INNAME', type=str, default='',
                    help='input file for mesh in Gmsh format (.msh)')
parser.add_argument('-lidscale', type=float, default=1.0, metavar='X',
                    help='scale for lid velocity (rightward positive; default=1.0)')
parser.add_argument('-mx', type=int, default=3, metavar='MX',
                    help='number of grid points in x-direction (uniform case)')
parser.add_argument('-my', type=int, default=3, metavar='MY',
                    help='number of grid points in y-direction (uniform case)')
parser.add_argument('-mu', type=float, default=1.0, metavar='MU',
                    help='dynamic viscosity (default=1.0)')
parser.add_argument('-nobase', action='store_true', default=False,
                    help='use problem with stress-free boundary condition on base')
parser.add_argument('-o', metavar='OUTNAME', type=str, default='',
                    help='output file name ending with .pvd')
parser.add_argument('-pdegree', type=int, default=1, metavar='L',
                    help='polynomial degree for pressure (default=1)')
parser.add_argument('-quad', action='store_true', default=False,
                    help='use quadrilateral finite elements')
parser.add_argument('-refine', type=int, default=0, metavar='R',
                    help='number of refinement levels (e.g. for GMG)')
parser.add_argument('-schurgmg', metavar='PKG', default='',
                    help='choose a Schur+GMG PC solver package: see text for options')
parser.add_argument('-showinfo', action='store_true', default=False,
                    help='print function space sizes and solution norms (useful for testing)')
parser.add_argument('-udegree', type=int, default=2, metavar='K',
                    help='polynomial degree for velocity (default=2)')
args, unknown = parser.parse_known_args()
assert not (args.analytical and args.nobase), 'conflict in problem choice options'

# read Gmsh mesh or create uniform mesh
if len(args.i) > 0:
    assert (not args.analytical), 'Gmsh file not allowed for -analytical problem'
    assert (not args.nobase), 'Gmsh file not allowed for -nobase problem'
    PETSc.Sys.Print('reading mesh from %s ...' % args.i)
    mesh = Mesh(args.i)
    meshstr = ''
    other = (41,)
    lid = (40,)
else:
    mx, my = args.mx, args.my
    mesh = UnitSquareMesh(mx-1, my-1, quadrilateral=args.quad)
    mx, my = (mx-1) * 2**args.refine + 1, (my-1) * 2**args.refine + 1
    meshstr = ' on %d x %d grid' % (mx,my)
    # boundary i.d.s:    ---4---
    #                    |     |
    #                    1     2
    #                    |     |
    #                    ---3---
    if args.nobase:
        other = (1,2)
    else:
        other = (1,2,3)
    lid = (4,)

# enable GMG using hierarchy
if args.refine > 0:
    hierarchy = MeshHierarchy(mesh, args.refine)
    mesh = hierarchy[-1]     # the fine mesh
    if len(args.i) > 0:
        meshstr += ' with %d levels uniform refinement' % args.refine
x,y = SpatialCoordinate(mesh)
mesh._plex.viewFromOptions('-dm_view')

# define mixed finite elements
V = VectorFunctionSpace(mesh, 'CG', degree=args.udegree)
if args.dp:
    W = FunctionSpace(mesh, 'DG', degree=args.pdegree)
else:
    W = FunctionSpace(mesh, 'CG', degree=args.pdegree)
Z = V * W

# define body force and Dir. boundary condition (on velocity only)
#     note: UFL as_vector() takes UFL expressions and combines
if args.analytical:
    assert (len(args.i) == 0)  # require UnitSquareMesh
    assert (args.mu == 1.0)
    f_body = as_vector([ 28.0 * pi*pi * sin(4.0*pi*x) * cos(4.0*pi*y), \
                       -36.0 * pi*pi * cos(4.0*pi*x) * sin(4.0*pi*y)])
    u_12 = Function(V).interpolate(as_vector([0.0,-sin(4.0*pi*y)]))
    u_34 = Function(V).interpolate(as_vector([sin(4.0*pi*x),0.0]))
    bcs = [ DirichletBC(Z.sub(0), u_12, (1,2)),
            DirichletBC(Z.sub(0), u_34, (3,4)) ]
else:
    f_body = Constant((0.0, 0.0))  # no body force in lid-driven cavity
    u_noslip = Constant((0.0, 0.0))
    ux_lid = args.lidscale * x * (1.0 - x)
    u_lid = Function(V).interpolate(as_vector([ux_lid,0.0]))
    bcs = [ DirichletBC(Z.sub(0), u_noslip, other),
            DirichletBC(Z.sub(0), u_lid,    lid)   ]

# some cases have a null space
if args.nobase:
    ns = None
else:
    # Dirichlet-only boundary conds on velocity therefore set nullspace to constant pressure
    ns = MixedVectorSpaceBasis(Z, [Z.sub(0), VectorSpaceBasis(constant=True)])

# define weak form
up = Function(Z)
u,p = split(up)
v,q = TestFunctions(Z)
F = (args.mu * inner(grad(u), grad(v)) - p * div(v) - div(u) * q \
     - inner(f_body,v)) * dx

# solver notes:
# 1. -s_pc_fieldsplit_type schur
#       This is the ONLY viable fieldsplit type because others (i.e. additive,
#       multiplicative, and symmetric_multiplicative) all fail because diagonal
#       pressure block is zero (non-invertible).
# 2. -s_pc_fieldsplit_schur_factorization_type diag
#       The Murphy et al 2000 theorem applies to MINRES + (this option).  Note
#       the default for diag is -pc_fieldsplit_schur_scale -1.0.  We do NOT
#       want this sign flip when using Mass for preconditioning because Mass
#       is already SPD.
# 3. For preconditioning of the Schur block  S = - B A^-1 B^T  we may use a
#       viscosity-weighted form of the mass matrix to approximate -S^{-1}.
#       The reference for how to do this in Firedrake is
#         https://www.firedrakeproject.org/demos/geometric_multigrid.py.html
#       The class Mass below, and the options below, are from this source.
# 4. -s_pc_fieldsplit_schur_precondition selfp
#       When not using Mass it seems to be faster to go ahead and *assemble*
#       the preconditioner for the A11 block.  This option does so, but only
#       inverts the diagonal of A00, so S \approx - B inv(diag(A)) B^T
# 5. The "diag_mass" PC combination below uses bjacobi+icc, which is possible
#       because the mass matrix is SPD.  However, testing shows that for the
#       "lower_mass" combination jacobi is much better *for fine grids* than
#       bjacobi+icc.  It is not clear why this is so.

class Mass(AuxiliaryOperatorPC):

    def form(self, pc, test, trial):
        a = (1.0/args.mu) * inner(test, trial)*dx
        bcs = None
        return (a, bcs)

# Schur + GMG based solver packages
common = {'pc_type': 'fieldsplit',
          'pc_fieldsplit_type': 'schur',
          'fieldsplit_0_ksp_type': 'preonly',
          'fieldsplit_0_pc_type': 'mg',
          'fieldsplit_1_ksp_type': 'preonly'}

pacs = {# diagonal Schur with mass-matrix PC on pressures; use minres or gmres
        'diag_mass':
           {'pc_fieldsplit_schur_fact_type': 'diag',
            'pc_fieldsplit_schur_precondition': 'a11',
            'pc_fieldsplit_schur_scale': 1.0,
            'fieldsplit_1_pc_type': 'python',
            'fieldsplit_1_pc_python_type': '__main__.Mass',
            'fieldsplit_1_aux_pc_type': 'bjacobi',
            'fieldsplit_1_aux_sub_pc_type': 'icc'},
        # diagonal Schur with with identity as PC on pressures; use minres or gmres
        'diag_eye':
           {'pc_fieldsplit_schur_fact_type': 'diag',
            'pc_fieldsplit_schur_precondition': 'a11',
            'pc_fieldsplit_schur_scale': 1.0,
            'fieldsplit_1_pc_type': 'jacobi'},
        # diagonal Schur using "selfp" PC on pressures; use minres or gmres
        'diag':
           {'pc_fieldsplit_schur_fact_type': 'diag',
            'pc_fieldsplit_schur_precondition': 'selfp',
            'pc_fieldsplit_schur_scale': -1.0,
            'fieldsplit_1_pc_type': 'jacobi'},
        # diagonal Schur with (very slow) "full" assembly of S and then
        # cholesky factorization of it; fails if S has a kernel
        # only serial; use minres or gmres
        'diag_full_cholesky':
           {'pc_fieldsplit_schur_fact_type': 'diag',
            'pc_fieldsplit_schur_precondition': 'full',
            'pc_fieldsplit_schur_scale': -1.0,
            'fieldsplit_1_pc_type': 'cholesky'},
        # lower-triangular Schur with mass-matrix PC on pressures; use gmres
        'lower_mass':
           {'pc_fieldsplit_schur_fact_type': 'lower',
            'pc_fieldsplit_schur_precondition': 'a11',
            'fieldsplit_1_pc_type': 'python',
            'fieldsplit_1_pc_python_type': '__main__.Mass',
            'fieldsplit_1_aux_pc_type': 'bjacobi',
            'fieldsplit_1_aux_sub_pc_type': 'icc'},
        # lower-triangular Schur with identity as PC on pressures; use gmres
        'lower_eye':
           {'pc_fieldsplit_schur_fact_type': 'lower',
            'pc_fieldsplit_schur_precondition': 'a11',
            'fieldsplit_1_pc_type': 'jacobi'},
        # lower-triangular Schur using "selfp" PC on pressures; use gmres or fgmres
        'lower':
           {'pc_fieldsplit_schur_fact_type': 'lower',
            'pc_fieldsplit_schur_precondition': 'selfp',
            'fieldsplit_1_pc_type': 'jacobi'},
       }

# select solver package
sparams = {}
if len(args.schurgmg) > 0:
    sparams.update(common)
    try:
        sparams.update(pacs[args.schurgmg])
    except KeyError:
        print('ERROR: invalid solver package choice')
        print('       choices are %s' % list(pacs.keys()))
        sys.exit(1)
sparams.update({'snes_type': 'ksponly'})  # applies to all

# describe mixed FE method
uFEstr = '%s^%d' % (['P','Q'][args.quad],args.udegree)
pFEstr = '%s^%d' % (['P','Q'][args.quad],args.pdegree)
if args.dp:
    mixedname = 'CD'
else:
    if args.pdegree == args.udegree - 1:
        mixedname = 'Taylor-Hood'
    else:
        mixedname = ''
Z = V * W
PETSc.Sys.Print('solving%s with %s x %s %s elements ...' \
                % (meshstr,uFEstr,pFEstr,mixedname))

if len(args.schurgmg) > 0 and args.showinfo:
    PETSc.Sys.Print('  Schur+GMG PC package %s' % args.schurgmg)

# actually solve
solve(F == 0, up, bcs=bcs, nullspace=ns,
      options_prefix='s', solver_parameters=sparams)
u,p = up.split()

# numerical error (if possible)
if args.analytical:
    xexact = sin(4.0*pi*x) * cos(4.0*pi*y)
    yexact = -cos(4.0*pi*x) * sin(4.0*pi*y)
    # compare Logg et al 2012, Fig 20.11; degree 10 is not necessary but same
    # degree as computation spaces will yield wrong rates
    Vhigh = VectorFunctionSpace(mesh, 'CG', degree=args.udegree+2)
    Whigh = FunctionSpace(mesh, 'CG', degree=args.pdegree+2)
    u_exact = Function(Vhigh).interpolate(as_vector([xexact,yexact]))
    p_exact = Function(Whigh).interpolate(pi * cos(4.0*pi*x) * cos(4.0*pi*y))
    uerr = sqrt(assemble(dot(u - u_exact, u - u_exact) * dx))
    perr = sqrt(assemble(dot(p - p_exact, p - p_exact) * dx))
    PETSc.Sys.Print('  numerical errors: |u-uexact|_h = %.3e, |p-pexact|_h = %.3e' \
                    % (uerr, perr))

# optionally print number of degrees of freedom and solution norms
if args.showinfo:
    n_u,n_p = V.dim(),W.dim()
    PETSc.Sys.Print('  sizes: n_u = %d, n_p = %d, N = %d' % (n_u,n_p,n_u+n_p))
    uL2 = sqrt(assemble(dot(u, u) * dx))
    pL2 = sqrt(assemble(dot(p, p) * dx))
    PETSc.Sys.Print('  solution norms: |u|_h = %.5e, |p|_h = %.5e' % (uL2, pL2))

# optionally save to .pvd file viewable with Paraview
if len(args.o) > 0:
    PETSc.Sys.Print('saving to %s ...' % args.o)
    u.rename('velocity')
    p.rename('pressure')
    File(args.o).write(u,p)

