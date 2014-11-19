
static char help[] = "Solves a structured-grid Poisson problem with DMDA and KSP.\n\n";

// SEE ALSO:  c4poisson.c
// IT IS SIMILAR BUT HAS MULTIGRID ABILITY BECAUSE OPERATOR A IS GENERATED AT
// EACH LEVEL THROUGH
//     KSPSetDM(ksp,(DM)da) ... KSPSetComputeOperators(ksp,ComputeJacobian,&user)

// SHOW MAT DENSE:  ./c2poisson -da_grid_x 3 -da_grid_y 3 -a_mat_view ::ascii_dense
// SHOW MAT GRAPHICAL:  ./c2poisson -a_mat_view draw -draw_pause 5

// CONVERGENCE:
//   for NN in 5 9 17 33 65 129 257; do ./c2poisson -da_grid_x $NN -da_grid_y $NN -ksp_rtol 1.0e-8 -ksp_type cg; done

// SAME CONVERGENCE USING -da_refine:
//   for NN in 1 2 3 4 5 6; do ./c2poisson -da_grid_x 5 -da_grid_y 5 -ksp_rtol 1.0e-8 -ksp_type cg -da_refine $NN; done

// VISUALIZATION OF SOLUTION: mpiexec -n 6 ./c2poisson -da_grid_x 129 -da_grid_y 129 -ksp_type cg -ksp_monitor_solution

// SHOW KSP STRUCTURE:
//   ./c2poisson -ksp_view                              GMRES WITH IC(0)
//   ./c2poisson -ksp_view -ksp_type cg                 CG WITH IC(0)
//   ./c2poisson -ksp_view -ksp_type cg -pc_type none   UNPRECONDITIONED CG

// DIRECT LINEAR SOLVERS:
//   LU ALGORITHM: ./c2poisson -ksp_type preonly -pc_type lu
//   CHOLESKY ALGORITHM: ./c2poisson -ksp_type preonly -pc_type cholesky
//   SHOWS CG CAN GIVE SAME RESIDUAL: ./c2poisson -ksp_type cg -ksp_rtol 1.0e-14

// UNPRECONDITIONED CG ALGORITHM:
//   ./c2poisson -ksp_type cg -pc_type none -ksp_view  # JUST SHOW KSP STRUCTURE
//   ./c2poisson -da_grid_x 257 -da_grid_y 257 -ksp_type cg -pc_type none -log_summary
//   (compare Elman p.72 and Algorithm 2.1 = cg: "The computational work of one
//   iteration is two inner products, three vector updates, and one matrix-vector
//   product." THIS IS WHAT I SEE!!)

// MORE CG:  look at iterations in
//   for NN in 5 9 17 33 65 129 257; do ./c2poisson -da_grid_x $NN -da_grid_y $NN -ksp_rtol 1.0e-8 -ksp_type cg -pc_type none; done
// and look at iterations in
//   for NN in 5 9 17 33 65 129 257; do ./c2poisson -da_grid_x $NN -da_grid_y $NN -ksp_rtol 1.0e-8 -ksp_type cg; done
// IN BOTH CASES ITERATIONS (ASYMPTOTICALLY) DOUBLE WITH EACH GRID REFINEMENT
//   (compare Elman p. 76: "...suggests that for uniformly refined grids, the
//   number of CG iterations required to meet a fixed tolerance will approximately
//   double with each grid refinement"
//   and compare Elman p. 82: "One known result [ABOUT IC(0) PRECONDITIONING USED
//   IN SECOND CASE ABOVE] is that the asymptotic behavior of the condition number
//   using IC(0) preconditioning is unchanged: \kappa(M^{-1} A) = O(h^{-2})."
//   THIS IS WHAT I SEE!!)

// MINRES VS CG:
//   time ./c2poisson -ksp_type cg -pc_type icc -da_grid_x 500 -da_grid_y 500
//   time ./c2poisson -ksp_type minres -pc_type icc -da_grid_x 500 -da_grid_y 500
//   (compare Elman p. 88: "Indeed, when solving discrete Poisson problems the
//   the convergence of MINRES is almost identical to that of CG"  THIS IS WHAT I SEE!!)

// PERFORMANCE ANALYSIS:
//   export PETSC_ARCH=linux-gnu-opt
//   make c2poisson
//   ./c2poisson -da_grid_x 1025 -da_grid_y 1025 -ksp_type cg -log_summary|grep "Solve: "
//   mpiexec -n 6 ./c2poisson -da_grid_x 1025 -da_grid_y 1025 -ksp_type cg -log_summary|grep "Solve: "

// PERFORMANCE ON CONVERGENCE PATH:
//   for NN in 5 9 17 33 65 129 257; do ./c2poisson -da_grid_x $NN -da_grid_y $NN -ksp_rtol 1.0e-8 -ksp_type cg -log_summary|grep "Time (sec):"; done

// WEAK SCALING IN TERMS OF FLOPS ONLY:
//   for kk in 0 1 2 3; do NN=$((50*(2**$kk))); MM=$((2**(2*$kk))); cmd="mpiexec -n $MM ./c2poisson -da_grid_x $NN -da_grid_y $NN -ksp_rtol 1.0e-8 -ksp_type cg -log_summary"; echo $cmd; $cmd |'grep' "Flops:  "; echo; done


#include <math.h>
#include <petscdmda.h>
#include <petscksp.h>
#include "structuredpoisson.h"

//CREATE
int main(int argc,char **args)
{
  PetscErrorCode ierr;
  PetscInitialize(&argc,&args,(char*)0,help);

  // default size (10 x 10) can be changed using -da_grid_x M -da_grid_y N
  DM  da;
  ierr = DMDACreate2d(PETSC_COMM_WORLD,
                DM_BOUNDARY_NONE, DM_BOUNDARY_NONE, DMDA_STENCIL_STAR,
                -10,-10,PETSC_DECIDE,PETSC_DECIDE,1,1,NULL,NULL,
                &da); CHKERRQ(ierr);
  ierr = DMDASetUniformCoordinates(da,0.0,1.0,0.0,1.0,-1.0,-1.0); CHKERRQ(ierr);

  // create linear system matrix A
  Mat  A;
  ierr = DMCreateMatrix(da,&A);CHKERRQ(ierr);
  ierr = MatSetOptionsPrefix(A,"a_"); CHKERRQ(ierr);
  ierr = MatSetFromOptions(A); CHKERRQ(ierr);

  // create right-hand-side (RHS) b, approx solution u, exact solution uexact
  Vec  b,u,uexact;
  ierr = DMCreateGlobalVector(da,&b);CHKERRQ(ierr);
  ierr = VecDuplicate(b,&u); CHKERRQ(ierr);
  ierr = VecDuplicate(b,&uexact); CHKERRQ(ierr);

  // fill known vectors
  ierr = formExact(da,uexact); CHKERRQ(ierr);
  ierr = formRHS(da,b); CHKERRQ(ierr);

  // assemble linear system
  PetscLogStage  stage; //STRIP
  ierr = PetscLogStageRegister("Matrix Assembly", &stage); CHKERRQ(ierr); //STRIP
  ierr = PetscLogStagePush(stage); CHKERRQ(ierr); //STRIP
  ierr = formdirichletlaplacian(da,1.0,A); CHKERRQ(ierr);
  ierr = PetscLogStagePop();CHKERRQ(ierr); //STRIP
//ENDCREATE

//SOLVE
  // create linear solver context
  KSP  ksp;
  ierr = KSPCreate(PETSC_COMM_WORLD,&ksp); CHKERRQ(ierr);
  ierr = KSPSetOperators(ksp,A,A); CHKERRQ(ierr);
  ierr = KSPSetFromOptions(ksp); CHKERRQ(ierr);

  // solve
  ierr = PetscLogStageRegister("Solve", &stage); CHKERRQ(ierr); //STRIP
  ierr = PetscLogStagePush(stage); CHKERRQ(ierr); //STRIP
  ierr = KSPSolve(ksp,b,u); CHKERRQ(ierr);
  ierr = PetscLogStagePop();CHKERRQ(ierr); //STRIP

  // report on grid and numerical error
  PetscScalar    errnorm;
  DMDALocalInfo  info;
  ierr = VecAXPY(u,-1.0,uexact); CHKERRQ(ierr);    // u <- u + (-1.0) uxact
  ierr = VecNorm(u,NORM_INFINITY,&errnorm); CHKERRQ(ierr);
  ierr = DMDAGetLocalInfo(da,&info);CHKERRQ(ierr);
  ierr = PetscPrintf(PETSC_COMM_WORLD,
             "on %d x %d grid:  error |u-uexact|_inf = %g\n",
             info.mx,info.my,errnorm); CHKERRQ(ierr);

  KSPDestroy(&ksp);
  VecDestroy(&u);  VecDestroy(&uexact);  VecDestroy(&b);
  MatDestroy(&A);
  DMDestroy(&da);
  PetscFinalize();
  return 0;
}
//ENDSOLVE

