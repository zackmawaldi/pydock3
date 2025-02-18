	subroutine newt(nit0,nitmx,nit1,nit2,nit3,iper,conv,omega,ichk0,donon)

c
c------------------------------------------------------------------
	include 'qdiffpar.h'
c------------------------------------------------------------------
	real*4 delmap(ngrid,ngrid,ngrid)
	real*4 eff(ngrid,ngrid,ngrid)
	logical iper(3),donon
	real*8 normf,normfp,normf1,lam,lamtol
c===========================================================================
c inexact Newton method for solving the nonlinear Poisson-Boltzmann eqn.
c based on Mike Holst's algorithm, implemented by Jonathan Hecht
c refined by kim sharp, oct 1993
c
c===========================================================================
c
c the NLPB eqn is: 
c
c  div(eps.grad phi) - K**2.sinh(phi) + 4pirho(fixed) = 0
c
c the finite difference form of the PB eqn is
c
c  sum(epsj.phij) - phi0.sum(epsj) - (K.h)**2.sinh(phi0) + 4pi.q0/h = 0
c
c where 0 refers the the values at tha grid point, the sum is over the j 
c index for the 6 neighbors, h is the spacing between grid points
c phi is stored in array phimap, eps is stored in array epsmap, 
c (K.h)**2 is stored in array debmap, and 4piq/h is stored in qmap
c
c The basic outline of the algorithm is:
c
c From current phi estimate, getf calculates the residual, fmap, the amount 
c that the lhs of the finite difference PB eqn. differes from 0 at each 
c lattice point.  The size of normf (the sum of the squares of the residual)
c is the criterion for convergence use din the outer, Newton method loop
c 
c The residuals are then used as the source term in the differential form
c of the PB equation used to estimate the correction to the potential, dphi:
c  
c  sum(epsj.dphij) - dphi0.sum(epsj) - (K.h)**2.dphi0.cosh(phi0) + fmap0 = 0
c
c where sinh(phi) has been replaced by its differential, dph.cosh(phi),
c and the charge term is replaced by the residual.  Delmap contains
c the correction to the potential, dph.  The residual of THIS
c equation is calculated in multin, stored in resid, and the estimated dphi is
c refined using the standard gauss-seidel relaxtaion scheme, where
c
c dphi0(new) = dphi0 + omega*resid/(sum(epsj) + (K.h)**2.cosh(phi0))
c
c until the normal of this residual, rnorm, reaches the required value.
c The relaxation parameter omega, and multigridding if necessary 
c aids convergence. omega=1.6 appears to be a good value.
c The potential is updated in the main newton loop
c using phi = phi+lambda*dphi.  Initially we try lambda = 1, and recalculate
c the normal, normf. If this is not decreased, then we try an ever smaller 
c lambda (decreasing it by a factor of a two each time) until the normal
c is decreased.  The nonlinear sinh and cosh terms are updates, the 
c differential equation is set up, and dphi solved for again, etc...
c until convergence in the outer loop is obtained.  Note that once we
c find the normal decreasing with a lambda of 1, it will always
c converge with lambda=1 and so we omit the lambda search thereafter
c
c===========================================================================

c set up arrays
c check for pts assigned charge that are in salt: blows nonlinaer equation up!
c
	start = cputime(0.0)
	print *,'using inexact Newton method to solve NLPB eqn...'
	print *,'dphi relaxation parameter: ',omega
	print *,'dphi multigrid iterations: ',nit0,nit1,nit2,nit3
	print *,' '
	nd1q1 = 0
	do k = 2,igrid-1
	  do j = 2,igrid-1
	    do i = 2,igrid-1
            deemap(i,j,k) = epsmap(i,j,k,1) + epsmap(i,j,k,2)
     &      + epsmap(i,j,k,3) + epsmap(i-1,j,k,1)
     &      + epsmap(i,j-1,k,2) + epsmap(i,j,k-1,3) 
            delmap(i,j,k) = 0.0
		phimap(i,j,k) = 0.
		if((abs(qmap(i,j,k)).gt.0.).and.(debmap(i,j,k).gt.0.))then
		  nd1q1 = nd1q1+1
		  debmap(i,j,k) = 0.
		end if
	    end do
	  end do
	end do
	if(nd1q1.gt.0)then
	  print *,'WARNING: # of charged points in salt: ',nd1q1
	  print *,'these were set out of salt; scale should be increased'
	end if
c
c intialize 'source' term of PB eqn., which is residual
c and calculate its normal
c
	ntot = 0
	lamtol = 1./2.**15
	call getf(eff,normf,donon)
c
c begin outer, newton iteration loop until convergence criteria is met 
c or maxmum iterations
c
	do while((normf.gt.conv).and.(ntot.lt.nitmx))
	  ntot = ntot + 1
	  print *,'NEWTON ITERATION#,norm: ',ntot,normf
c
c get new correction, delmap, to potential
c
	  call multin(nit0,nit1,nit2,nit3,omega,delmap,eff,normf,ichk0,donon)
c
c apply total correction to potential
c
	  do iz=2,igrid-1
	    do iy=2,igrid-1
	      do ix=2,igrid-1
	        phimap(ix,iy,iz)=phimap(ix,iy,iz)+delmap(ix,iy,iz)
	      end do
	    end do
        end do
c
c periodic boundary conditions
c
	  call doper(phimap,ngrid,igrid,iper)
c
c find new normal and check to see if converging
c
	  call getf(eff,normfp,donon)
	  if(normfp.ge.normf)then
c
c not converging, so subtract of half of correction until it does
c
	    lam = 0.5
	    do while((normfp.ge.normf).and.(lam.ge.lamtol))
	      do iz=2,igrid-1
	        do iy=2,igrid-1
	          do ix=2,igrid-1
	            phimap(ix,iy,iz)=phimap(ix,iy,iz)-lam*delmap(ix,iy,iz)
	          end do
	        end do
            end do
	      call doper(phimap,ngrid,igrid,iper)
c
c get the new residual, its normal and check lambda
c
	      call getf(eff,normfp,donon)
	      print *,'trial lambda, normal: ',lam,normfp
	      lam = lam/2.
	    end do
	    if(lam.lt.lamtol)print *,'WARNING: did not converge on lambda'
	  end if
	  normf = normfp
	end do
	fin = cputime(start)
	print *,'final normal: ',normf
	if(ntot.ge.nitmx)print *,'WARNING: max # of newton iterations: ',ntot
	print *,'total time for newton iterations: ',fin
	return
	end

	subroutine getf(fmap,normf,donon)
c
c this gets the new source term for the current newton iteration
c
c-------------------------------------------------
	include 'qdiffpar.h'
c-------------------------------------------------
	real*4 fmap(ngrid,ngrid,ngrid),pold,pold2
	real*8 normf
	logical donon
c-------------------------------------------------
	normf = 0.
	if(donon)then
        do iz = 2, igrid-1
          do iy = 2, igrid-1
            do ix = 2, igrid-1
		  pold = phimap(ix,iy,iz)
		  fmap(ix,iy,iz) = 
     &          phimap(ix,iy,iz)*deemap(ix,iy,iz) - qmap(ix,iy,iz)
     &	  - phimap(ix-1,iy,iz)*epsmap(ix-1,iy,iz,1)
     &        - phimap(ix+1,iy,iz)*epsmap(ix,iy,iz,1)
     &        - phimap(ix,iy-1,iz)*epsmap(ix,iy-1,iz,2)
     &        - phimap(ix,iy+1,iz)*epsmap(ix,iy,iz,2)
     &        - phimap(ix,iy,iz-1)*epsmap(ix,iy,iz-1,3)
     &        - phimap(ix,iy,iz+1)*epsmap(ix,iy,iz,3)
	        if(debmap(ix,iy,iz).ne.0.) then
                pold= max(phimap(ix,iy,iz),-15.)
	          pold= min(pold,15.)  
		    fmap(ix,iy,iz) = fmap(ix,iy,iz) + debmap(ix,iy,iz)*sinh(pold)
	        end if
		  normf = normf + fmap(ix,iy,iz)**2
	      end do
	    end do
	  end do
	else
        do iz = 2, igrid-1
          do iy = 2, igrid-1
            do ix = 2, igrid-1
		  pold = phimap(ix,iy,iz)
		  fmap(ix,iy,iz) = 
     &          phimap(ix,iy,iz)*(deemap(ix,iy,iz)+debmap(ix,iy,iz))
     &        - qmap(ix,iy,iz)
     &	  - phimap(ix-1,iy,iz)*epsmap(ix-1,iy,iz,1)
     &        - phimap(ix+1,iy,iz)*epsmap(ix,iy,iz,1)
     &        - phimap(ix,iy-1,iz)*epsmap(ix,iy-1,iz,2)
     &        - phimap(ix,iy+1,iz)*epsmap(ix,iy,iz,2)
     &        - phimap(ix,iy,iz-1)*epsmap(ix,iy,iz-1,3)
     &        - phimap(ix,iy,iz+1)*epsmap(ix,iy,iz,3)
		  normf = normf + fmap(ix,iy,iz)**2
	      end do
	    end do
	  end do
	end if
	return
	end

	subroutine getcoh(coh,donon)
c
c calculates the hyperbolic cosines for this iteration
c-------------------------------------------------
	include 'qdiffpar.h'
c-------------------------------------------------
	real*4 coh(ngrid,ngrid,ngrid)
	logical donon
c-------------------------------------------------------
	if(donon)then
        do iz = 2, igrid-1
          do iy = 2, igrid-1
            do ix = 2, igrid-1
	        if (debmap(ix,iy,iz).eq.0.) then
	          coh(ix,iy,iz) = 0.
	        else
                temp= max(phimap(ix,iy,iz),-15.)
	          temp= min(temp,15.) 
	          coh(ix,iy,iz) = cosh(temp)*debmap(ix,iy,iz)
              end if
	      end do
	    end do
	  end do
	else
        do iz = 2, igrid-1
          do iy = 2, igrid-1
            do ix = 2, igrid-1
	          coh(ix,iy,iz) = debmap(ix,iy,iz)
	      end do
	    end do
	  end do
	end if
	return
	end

	subroutine multin(nit0,nit1s,nit2,nit3,omega,
     &delmap,eff,normf,ichk0,donon)
c
c-------------------------------------------------
	include 'qdiffpar.h'
c-------------------------------------------------
	parameter (nitmx=300)
	parameter (ngrid1 = (ngrid+1)/2)
	parameter (ngrid2 = (ngrid1+1)/2)
	parameter (ngrid3 = (ngrid2+1)/2)
c
c uppper level (0) arrays
c
	real*4 delmap(ngrid,ngrid,ngrid)
	real*4 eff(ngrid,ngrid,ngrid)
	real*4 rsumeps(ngrid,ngrid,ngrid)
	real*4 sumeps(ngrid,ngrid,ngrid)
	real*4 resid(ngrid,ngrid,ngrid)
	real*4 ddelmap(ngrid,ngrid,ngrid)
	real*4 dresid(ngrid,ngrid,ngrid)
	real*4 coh(ngrid,ngrid,ngrid) 
c
c level 1 arrays
c
	real*4 epsmap1(ngrid1,ngrid1,ngrid1,3)
	real*4 eff1(ngrid1,ngrid1,ngrid1)
	real*4 rsumeps1(ngrid1,ngrid1,ngrid1)
	real*4 sumeps1(ngrid1,ngrid1,ngrid1)
	real*4 resid1(ngrid1,ngrid1,ngrid1)
	real*4 ddelmap1(ngrid1,ngrid1,ngrid1)
	real*4 debmap1(ngrid1,ngrid1,ngrid1)
	real*4 delmap1(ngrid1,ngrid1,ngrid1)
	real*4 coh1(ngrid1,ngrid1,ngrid1) 
c
c level 2 arrays
c
	real*4 epsmap2(ngrid2,ngrid2,ngrid2,3)
	real*4 eff2(ngrid2,ngrid2,ngrid2)
	real*4 rsumeps2(ngrid2,ngrid2,ngrid2)
	real*4 sumeps2(ngrid2,ngrid2,ngrid2)
	real*4 resid2(ngrid2,ngrid2,ngrid2)
	real*4 ddelmap2(ngrid2,ngrid2,ngrid2)
	real*4 debmap2(ngrid2,ngrid2,ngrid2)
	real*4 delmap2(ngrid2,ngrid2,ngrid2)
	real*4 coh2(ngrid2,ngrid2,ngrid2) 
c
c level 3 arrays
c
	real*4 epsmap3(ngrid3,ngrid3,ngrid3,3)
	real*4 eff3(ngrid3,ngrid3,ngrid3)
	real*4 rsumeps3(ngrid3,ngrid3,ngrid3)
	real*4 sumeps3(ngrid3,ngrid3,ngrid3)
	real*4 resid3(ngrid3,ngrid3,ngrid3)
	real*4 debmap3(ngrid3,ngrid3,ngrid3)
	real*4 delmap3(ngrid3,ngrid3,ngrid3)
	real*4 coh3(ngrid3,ngrid3,ngrid3) 
c
	character*8 hour
	real*8 normf,rnorm,rnorm1,rnorm2,rnorm3
	logical donon
	data zero / 0. /
c-------------------------------------------------------
	start = cputime(0.0)
c-------------------------------------------------------
c
c start	
c
	nit1 = abs(nit1s)
	igrid1 = 0
	igrid2 = 0
	igrid3 = 0
c	print *,' '
	if(nit1.gt.0)then
	  if(mod(igrid-1,16).eq.0)then
	    igrid1 = (igrid+1)/2
	    igrid2 = (igrid1+1)/2
	    igrid3 = (igrid2+1)/2
c	    print *,'using 4 level multigrid'
	  else if(mod(igrid-1,8).eq.0)then
	    nit3 = 0
	    igrid1 = (igrid+1)/2
	    igrid2 = (igrid1+1)/2
	    igrid3 = 0
c	    print *,'using 3 level multigrid'
	  else if(mod(igrid-1,4).eq.0)then
	    nit2 = 0
	    nit3 = 0
	    igrid1 = (igrid+1)/2
	    igrid2 = 0
	    igrid3 = 0
c	    print *,'using 2 level multigrid'
	  else 
	    nit1 = 0
	    nit2 = 0
	    nit3 = 0
	    igrid1 = 0
	    igrid2 = 0
	    igrid3 = 0
c	    print *,'multigridding switched off'
	  end if
	else
c	  print *,'multigridding switched off'
	end if
c	print *,'grid dimensions: ',ngrid,ngrid1,ngrid2,ngrid3
c	print *,'grid sizes     : ',igrid,igrid1,igrid2,igrid3
c	print *,'level 0,1,2,3 iterations: ',nit0,nit1,nit2,nit3
c===============================
c
c set up coefficient arrays
c
c===============================
c level 0
c===============================
c 	get cosh * debmap array
	call getcoh(coh,donon)
	call setarr(ddelmap,ngrid,igrid,zero)
	call setarr(delmap,ngrid,igrid,zero)
	do k = 2,igrid-1
	  do j = 2,igrid-1
	    do i = 2,igrid-1
		sumeps(i,j,k) = deemap(i,j,k)+coh(i,j,k)
		rsumeps(i,j,k) = 1./sumeps(i,j,k)
	    end do
	  end do
	end do
c===============================
c level 1
c===============================
c
c coarse epsmap by harmonic averaging
c
	call hrmav(epsmap,ngrid,igrid,epsmap1,ngrid1,igrid1)
c
c coarse debmap directly
c
	ibound = 1
	call transfr(debmap,ngrid,igrid,debmap1,ngrid1,igrid1,ibound)
	call transfr(coh,ngrid,igrid,coh1,ngrid1,igrid1,ibound)
c
c secondary coefficients
c
	call setarr(ddelmap1,ngrid1,igrid1,zero)
	do k = 2,igrid1-1
	  do j = 2,igrid1-1
	    do i = 2,igrid1-1
		delmap1(i,j,k) = 0.
		debmap1(i,j,k) = debmap1(i,j,k)*4.
		coh1(i,j,k) = coh1(i,j,k)*4.
		sumeps1(i,j,k) = epsmap1(i,j,k,1) + epsmap1(i,j,k,2) 
     &			  + epsmap1(i,j,k,3) + epsmap1(i-1,j,k,1) 
     &			  + epsmap1(i,j-1,k,2) + epsmap1(i,j,k-1,3)
     &			  + coh1(i,j,k)
		rsumeps1(i,j,k) = 1./sumeps1(i,j,k)
	    end do
	  end do
	end do
c===============================
c level 2
c===============================
	call hrmav(epsmap1,ngrid1,igrid1,epsmap2,ngrid2,igrid2)
	ibound = 1
	call transfr(debmap1,ngrid1,igrid1,debmap2,ngrid2,igrid2,ibound)
	call transfr(coh1,ngrid1,igrid1,coh2,ngrid2,igrid2,ibound)
	do k = 2,igrid2-1
	  do j = 2,igrid2-1
	    do i = 2,igrid2-1
		delmap2(i,j,k) = 0.
		debmap2(i,j,k) = debmap2(i,j,k)*4.
		coh2(i,j,k) = coh2(i,j,k)*4.
		sumeps2(i,j,k) = epsmap2(i,j,k,1) + epsmap2(i,j,k,2) 
     &			  + epsmap2(i,j,k,3) + epsmap2(i-1,j,k,1) 
     &			  + epsmap2(i,j-1,k,2) + epsmap2(i,j,k-1,3)
     &			  + coh2(i,j,k)
		rsumeps2(i,j,k) = 1./sumeps2(i,j,k)
	    end do
	  end do
	end do
c===============================
c level 3
c===============================
	call hrmav(epsmap2,ngrid2,igrid2,epsmap3,ngrid3,igrid3)
	ibound = 1
	call transfr(debmap2,ngrid2,igrid2,debmap3,ngrid3,igrid3,ibound)
	call transfr(coh2,ngrid2,igrid2,coh3,ngrid3,igrid3,ibound)
	do k = 2,igrid3-1
	  do j = 2,igrid3-1
	    do i = 2,igrid3-1
		delmap3(i,j,k) = 0.
		debmap3(i,j,k) = debmap3(i,j,k)*4.
		coh3(i,j,k) = coh3(i,j,k)*4.
		sumeps3(i,j,k) = epsmap3(i,j,k,1) + epsmap3(i,j,k,2) 
     &			  + epsmap3(i,j,k,3) + epsmap3(i-1,j,k,1) 
     &			  + epsmap3(i,j-1,k,2) + epsmap3(i,j,k-1,3)
     &			  + coh3(i,j,k)
		rsumeps3(i,j,k) = 1./sumeps3(i,j,k)
	    end do
	  end do
	end do
c---------------------------------------------------------------
	om0 = omega
c
c start level 1,2 relaxation parameters at 1.8, max val is 1.9
c
	om1 = 1.6
	om2 = 1.6
	om3 = 1.6
c	print *,'level 0,1,2,3 omegas: ',om0,om1,om2,om3
c
	ntot = 0
	icheck=0
 	print *,'LEVEL  #it    Norm of dphi residual'
300	continue ! big loop over grids
c
c=============================================
c smoooth for nit0 iterations at LEVEL 0
c 1st time thru do at least n2 iterations 
c=============================================
	do n = 1,nit0
	  ntot=ntot+1
	  if(ntot.gt.nitmx)then
	    print *,'WARNING: exceeding maximum # of iterations: ',nitmx
	    ntot = nitmx
	    goto 301
	  end if
c
c calculate top level residuals and update using chequerboard gauss-seidel
c do odd parity points first iodd = 0, 
c do even parity points last iodd = 1, so that
c points on coarser grid (which are odd) are left with the residuals
c
	  do iodd = 0,1
	    call getres(delmap,epsmap,sumeps,eff,resid,ngrid,igrid,iodd,rnorm)
	    call gausei(delmap,resid,rsumeps,ngrid,igrid,iodd,om0)
	  end do
	  write(6,200)' 0      ',ntot,rnorm
c	  if (rnorm.le.normf) goto 301
200	  format(a,i3,f13.6)
c
c periodic boundary conditions
c
	end do
c
c update residual
c
	do iodd = 0,1
	    call getres(delmap,epsmap,sumeps,eff,resid,ngrid,igrid,iodd,rnorm)
	end do
	write(6,200)' 0      ',ntot,rnorm
	icheck=icheck+1
	if(icheck.eq.ichk0)then
	  icheck = 0
	  if (rnorm.le.normf) goto 301
	end if
c
c do level 1 and below only if multigrid iterations
c
	if(nit1.eq.0)goto 300
c==================================================
c transfer residuals to LEVEL 1 and solve for error
c==================================================
	ibound = 0
	call transfr(resid,ngrid,igrid,eff1,ngrid1,igrid1,ibound)
	call setarr(delmap1,ngrid1,igrid1,zero)
c==========================================
c presmooth at level 1
c==========================================
	do n = 1,nit1
	  do iodd = 0,1
	    call getres(delmap1,epsmap1,sumeps1,eff1,resid1,ngrid1,
     &    igrid1,iodd,rnorm1)
	    call gausei(delmap1,resid1,rsumeps1,ngrid1,igrid1,iodd,om1)
	  end do
	end do
	do iodd = 0,1
	  call getres(delmap1,epsmap1,sumeps1,eff1,resid1,
     &  ngrid1,igrid1,iodd,rnorm1)
	end do
	write(6,200)'    pre1      ',n,rnorm1
	if(nit2.eq.0)goto 400
c==================================================
c transfer residuals to LEVEL 2 and solve for error in error
c==================================================
	ibound = 0
	call transfr(resid1,ngrid1,igrid1,eff2,ngrid2,igrid2,ibound)
	call setarr(delmap2,ngrid2,igrid2,zero)
c==========================================
c presmooth at level 2
c==========================================
	do n = 1,nit2
	  do iodd = 0,1
	    call getres(delmap2,epsmap2,sumeps2,eff2,resid2,
     &    ngrid2,igrid2,iodd,rnorm2)
	    call gausei(delmap2,resid2,rsumeps2,ngrid2,igrid2,iodd,om2)
	  end do
	end do
	do iodd = 0,1
	  call getres(delmap2,epsmap2,sumeps2,eff2,resid2,
     &  ngrid2,igrid2,iodd,rnorm2)
	end do
	write(6,200)'      pre2      ',n,rnorm2
	if(nit3.eq.0)goto 500
c==================================================
c transfer residuals to LEVEL 3 and solve for error in error
c==================================================
	ibound = 0
	call transfr(resid2,ngrid2,igrid2,eff3,ngrid3,igrid3,ibound)
	call setarr(delmap3,ngrid3,igrid3,zero)
c==========================================
c smooth at level 3
c==========================================
	do n = 1,nit3
	  do iodd = 0,1
	    call getres(delmap3,epsmap3,sumeps3,eff3,resid3,
     &    ngrid3,igrid3,iodd,rnorm3)
	    call gausei(delmap3,resid3,rsumeps3,ngrid3,igrid3,iodd,om3)
	  end do
	end do
	do iodd = 0,1
	  call getres(delmap3,epsmap3,sumeps3,eff3,
     &  resid3,ngrid3,igrid3,iodd,rnorm3)
	end do
	write(6,200)'       post3      ',n,rnorm3
c===================================================
c transfer errors to level 2 and correct the error
c===================================================
	call intcrc(delmap3,ngrid3,igrid3,ddelmap2,epsmap2,
     &debmap2,ngrid2,igrid2)
	call addarr(delmap2,ddelmap2,ngrid2,igrid2)
c==========================================
c postsmooth at level 2
c==========================================
	do n = 1,nit2
	  do iodd = 0,1
	    call getres(delmap2,epsmap2,sumeps2,eff2,
     &    resid2,ngrid2,igrid2,iodd,rnorm2)
	    call gausei(delmap2,resid2,rsumeps2,ngrid2,igrid2,iodd,om2)
	  end do
	end do
	do iodd = 0,1
	  call getres(delmap2,epsmap2,sumeps2,eff2,
     &  resid2,ngrid2,igrid2,iodd,rnorm2)
	end do
	write(6,200)'     post2      ',n,rnorm2
500   continue
c===================================================
c transfer errors to level 1 and correct the error
c===================================================
	call intcrc(delmap2,ngrid2,igrid2,ddelmap1,
     &epsmap1,debmap1,ngrid1,igrid1)
	call addarr(delmap1,ddelmap1,ngrid1,igrid1)
c==============================
c postsmooth at level 1
c==============================
	do n = 1,nit1
	  do iodd = 0,1
	    call getres(delmap1,epsmap1,sumeps1,eff1,resid1,
     &    ngrid1,igrid1,iodd,rnorm1)
	    call gausei(delmap1,resid1,rsumeps1,ngrid1,igrid1,iodd,om1)
	  end do
	end do
	do iodd = 0,1
	  call getres(delmap1,epsmap1,sumeps1,eff1,resid1,
     &  ngrid1,igrid1,iodd,rnorm1)
	end do
	write(6,200)'   post1      ',n,rnorm1
400   continue
c===================================================
c transfer errors to level 0 and correct the potential
c===================================================
	call intcrc(delmap1,ngrid1,igrid1,ddelmap,epsmap,
     &debmap,ngrid,igrid)
	call addarr(delmap,ddelmap,ngrid,igrid)
c==========================================================================
c end of big loop 
c==========================================================================
	goto 300
301	continue   !here if converged at level 0
	finish = cputime(start)
	call time(hour)
c	write(6,*)'finished ',ntot,' iterations in ',finish
c	print *,' '
	end
c--------------------------------------------------------

	subroutine getres(delmap,epsmap,sumeps,eff,resid,ngrid,igrid,iodd,rnorm)
c
c calculate residual: res = f - L(func)
c do odd parity points first (iodd = 0->1, so that
c points on coarser grid are left with residuals
c
	real*4 resid(ngrid,ngrid,ngrid)
	real*4 eff(ngrid,ngrid,ngrid)
	real*4 sumeps(ngrid,ngrid,ngrid)
	real*4 delmap(ngrid,ngrid,ngrid)
	real*4 epsmap(ngrid,ngrid,ngrid,3)
	real*8 rnorm
c------------------------------------------------
c	print *,'iodd: ',iodd
	if(iodd.eq.0)rnorm = 0.
	do k = 2,igrid-1
	  do j = 2,igrid-1
c iodd = 0: do odd parity points
	    ist = mod(iodd+k+j+1,2)+2
	    do i = ist,igrid-1,2
		resid(i,j,k) = eff(i,j,k) + sumeps(i,j,k)*delmap(i,j,k)
     &	- delmap(i-1,j,k)*epsmap(i-1,j,k,1)
     &	- delmap(i+1,j,k)*epsmap(i,j,k,1)
     &	- delmap(i,j-1,k)*epsmap(i,j-1,k,2)
     &	- delmap(i,j+1,k)*epsmap(i,j,k,2)
     &	- delmap(i,j,k-1)*epsmap(i,j,k-1,3)
     &	- delmap(i,j,k+1)*epsmap(i,j,k,3)
		rnorm = rnorm + resid(i,j,k)**2
	    end do
	  end do
	end do
	return
	end

	subroutine gausei(delmap,resid,rsumeps,ngrid,igrid,iodd,omega)
c
c update potential estimate using Gauss-Seidel method
c with chequerboard order
c
	real*4 resid(ngrid,ngrid,ngrid)
	real*4 rsumeps(ngrid,ngrid,ngrid)
	real*4 delmap(ngrid,ngrid,ngrid)
c------------------------------------
	do k = 2,igrid-1
	  do j = 2,igrid-1
c iodd = 0: do odd parity points
	    ist = mod(iodd+k+j+1,2)+2
	    do i = ist,igrid-1,2
		delmap(i,j,k) = delmap(i,j,k) - omega*resid(i,j,k)*rsumeps(i,j,k)
	    end do
	  end do
	end do
	return
	end

	subroutine doper(phimap,ngrid,igrid,iper)
c
c if periodic boundary condition option
c force periodicity using wrap around update of boundary values:
c 2nd slice-->last
c last-1 slice-->first
c
	real*4 phimap(ngrid,ngrid,ngrid)
	logical iper(3)
c----------------------------------------
c
c z periodicity
c
	if(iper(3)) then
	  do iy = 2,igrid-1
	    do ix = 2,igrid-1
		 phimap(ix,iy,igrid) = phimap(ix,iy,2)
		 phimap(ix,iy,1) = phimap(ix,iy,igrid-1)
	    end do
	  end do
	end if
c
c y periodicity
c
	if(iper(2)) then
	  do iy = 2,igrid-1
	    do ix = 2,igrid-1
		 phimap(ix,igrid,iy) = phimap(ix,2,iy)
		 phimap(ix,1,iy) = phimap(ix,igrid-1,iy)
	    end do
	  end do
	end if
c
c x periodicity
c
	if(iper(1)) then
	  do iy = 2,igrid-1
	    do ix = 2,igrid-1
		 phimap(igrid,ix,iy) = phimap(2,ix,iy)
		 phimap(1,ix,iy) = phimap(igrid-1,ix,iy)
	    end do
	  end do
	end if
	return
	end

	subroutine transfr(array0,ngrid,igrid,array1,ngrid1,igrid1,ibound)
c
c transfer points from fine grid to coarse one. if ibound = 0
c then set boundary points to be 0, else transfer these too
c
	real*4 array0(ngrid,ngrid,ngrid)
	real*4 array1(ngrid1,ngrid1,ngrid1)
c------------------------------------------------
	do k = 2,igrid1-1
	  k0 = 2*k-1
	  do j = 2,igrid1-1
	    j0 = 2*j-1
	    do i = 2,igrid1-1
		i0 = 2*i-1
c straight transfer
		array1(i,j,k) = array0(i0,j0,k0)
c average
c		array1(i,j,k) = 0.5*array0(i0,j0,k0) + (
c     & array0(i0+1,j0,k0)+array0(i0-1,j0,k0) +
c     & array0(i0,j0+1,k0)+array0(i0,j0-1,k0) +
c     & array0(i0,j0,k0+1)+array0(i0,j0,k0-1))/12.
	    end do
	  end do
	end do
	if(ibound.eq.0)then
	  do k = 1,igrid1,igrid1-1
	    do j = 1,igrid1
		do i = 1,igrid1
		  array1(i,j,k) =  0.
		  array1(j,k,i) =  0.
		  array1(k,i,j) =  0.
		end do
	    end do
	  end do
	else
	  do k = 1,igrid1,igrid1-1
	    k0 = 2*k-1
	    do j = 1,igrid1
		j0 = 2*j-1
		do i = 1,igrid1
		  i0 = 2*i-1
		  array1(i,j,k) =  array0(i0,j0,k0)
		  array1(j,k,i) =  array0(j0,k0,i0)
		  array1(k,i,j) =  array0(k0,i0,j0)
		end do
	    end do
	  end do
	end if
	return
	end
c
	subroutine hrmav(epsmap,ngrid,igrid,epsmap1,ngrid1,igrid1)
c
c coarse eps is harmonic average of fine eps
c
	real*4 epsmap(ngrid,ngrid,ngrid,3)
	real*4 epsmap1(ngrid1,ngrid1,ngrid1,3)
c------------------------------------------------
	do k = 1,igrid1-1
	  k0 = 2*k-1
	  do j = 1,igrid1-1
	    j0 = 2*j-1
	    do i = 1,igrid1-1
		i0 = 2*i-1
		eps1 = epsmap(i0,j0,k0,1)
		eps2 = epsmap(i0+1,j0,k0,1)
		epsmap1(i,j,k,1) = 2.*eps1*eps2/(eps1+eps2)
c debug arithmetic av.
c		epsmap1(i,j,k,1) = (eps1+eps2)/2.
		eps1 = epsmap(i0,j0,k0,2)
		eps2 = epsmap(i0,j0+1,k0,2)
		epsmap1(i,j,k,2) = 2.*eps1*eps2/(eps1+eps2)
c debug arithmetic av.
c		epsmap1(i,j,k,2) = (eps1+eps2)/2.
		eps1 = epsmap(i0,j0,k0,3)
		eps2 = epsmap(i0,j0,k0+1,3)
		epsmap1(i,j,k,3) = 2.*eps1*eps2/(eps1+eps2)
c debug arithmetic av.
c		epsmap1(i,j,k,3) = (eps1+eps2)/2.
	    end do
	  end do
	end do
	return
	end

	subroutine setarr(array,ngrid,igrid,val)
c
c initialize array to value
c
	real*4 array(ngrid,ngrid,ngrid)
	do k = 1,igrid
	  do j = 1,igrid
	    do i = 1,igrid
		array(i,j,k) = val
	  end do
	  end do
	end do
	return
	end 

	subroutine addarr(array1,array2,ngrid,igrid)
	real*4 array1(ngrid,ngrid,ngrid)
	real*4 array2(ngrid,ngrid,ngrid)
	do k = 2,igrid-1
	  do j = 2,igrid-1
	    do i = 2,igrid-1
		array1(i,j,k) = array1(i,j,k)+array2(i,j,k)
	    end do
	  end do
	end do
	return
	end
c
	subroutine intcrc(delmap1,ngrid1,igrid1,ddelmap,epsmap,
     &debmap,ngrid,igrid)
c
c interpolate errors from coarse to fine grid and correct potential
c
	real*4 delmap1(ngrid1,ngrid1,ngrid1)
	real*4 ddelmap(ngrid,ngrid,ngrid)
	real*4 epsmap(ngrid,ngrid,ngrid,3)
	real*4 debmap(ngrid,ngrid,ngrid)
c------------------------------------------------
c
c common points
c
	do k = 2,igrid1-1
	  k0 = 2*k-1
	  do j = 2,igrid1-1
	    j0 = 2*j-1
	    do i = 2,igrid1-1
		i0 = 2*i-1
		ddelmap(i0,j0,k0) = delmap1(i,j,k)
	    end do
	  end do
	end do
c
c on line points 1st
c
	do k0 = 3,igrid-1,2
	  do j0 = 3,igrid-1,2
	    do i0 = 2,igrid-1,2
c x dir
		eps1 = epsmap(i0-1,j0,k0,1)
		eps2 = epsmap(i0,j0,k0,1)
		ddelmap(i0,j0,k0) =  
     &	(ddelmap(i0-1,j0,k0)*eps1 
     &      + ddelmap(i0+1,j0,k0)*eps2)/(eps1+eps2
     &      +debmap(i0,j0,k0))
c y dir
		eps1 = epsmap(j0,i0-1,k0,2)
		eps2 = epsmap(j0,i0,k0,2)
		ddelmap(j0,i0,k0) =  
     &	(ddelmap(j0,i0-1,k0)*eps1 
     &      + ddelmap(j0,i0+1,k0)*eps2)/(eps1+eps2
     &      +debmap(j0,i0,k0))
c z dir
		eps1 = epsmap(j0,k0,i0-1,3)
		eps2 = epsmap(j0,k0,i0,3)
		ddelmap(j0,k0,i0) =  
     &	(ddelmap(j0,k0,i0-1)*eps1 
     &      + ddelmap(j0,k0,i0+1)*eps2)/(eps1+eps2
     &      +debmap(j0,k0,i0))
	    end do
	  end do
	end do
c
c on plane points
c
	do k0 = 3,igrid-1,2
	  do j0 = 2,igrid-1,2
	    do i0 = 2,igrid-1,2
c xy plane
		eps1 = epsmap(i0-1,j0,k0,1)
		eps2 = epsmap(i0,j0,k0,1)
		eps3 = epsmap(i0,j0-1,k0,2)
		eps4 = epsmap(i0,j0,k0,2)
		ddelmap(i0,j0,k0) =  
     &	(ddelmap(i0-1,j0,k0)*eps1 + ddelmap(i0+1,j0,k0)*eps2 +
     &	ddelmap(i0,j0-1,k0)*eps3 + ddelmap(i0,j0+1,k0)*eps4)/
     &	(eps1+eps2+eps3+eps4+debmap(i0,j0,k0))
c yz plane
		eps1 = epsmap(k0,i0-1,j0,2)
		eps2 = epsmap(k0,i0,j0,2)
		eps3 = epsmap(k0,i0,j0-1,3)
		eps4 = epsmap(k0,i0,j0,3)
		ddelmap(k0,i0,j0) =  
     &	(ddelmap(k0,i0-1,j0)*eps1 + ddelmap(k0,i0+1,j0)*eps2 +
     &	ddelmap(k0,i0,j0-1)*eps3 + ddelmap(k0,i0,j0+1)*eps4)/
     &	(eps1+eps2+eps3+eps4+debmap(k0,i0,j0))
c zx plane
		eps1 = epsmap(j0,k0,i0-1,3)
		eps2 = epsmap(j0,k0,i0,3)
		eps3 = epsmap(j0-1,k0,i0,1)
		eps4 = epsmap(j0,k0,i0,1)
		ddelmap(j0,k0,i0) =  
     &	(ddelmap(j0,k0,i0-1)*eps1 + ddelmap(j0,k0,i0+1)*eps2 +
     &	ddelmap(j0-1,k0,i0)*eps3 + ddelmap(j0+1,k0,i0)*eps4)/
     &	(eps1+eps2+eps3+eps4+debmap(j0,k0,i0))
	    end do
	  end do
	end do
c
c center points
c
	do k0 = 2,igrid-1,2
	  do j0 = 2,igrid-1,2
	    do i0 = 2,igrid-1,2
		eps1 = epsmap(i0-1,j0,k0,1)
		eps2 = epsmap(i0,j0,k0,1)
		eps3 = epsmap(i0,j0-1,k0,2)
		eps4 = epsmap(i0,j0,k0,2)
		eps5 = epsmap(i0,j0,k0-1,3)
		eps6 = epsmap(i0,j0,k0,3)
		ddelmap(i0,j0,k0) =  
     &	(ddelmap(i0-1,j0,k0)*eps1 + ddelmap(i0+1,j0,k0)*eps2 +
     &	ddelmap(i0,j0-1,k0)*eps3 + ddelmap(i0,j0+1,k0)*eps4 +
     &	ddelmap(i0,j0,k0-1)*eps5 + ddelmap(i0,j0,k0+1)*eps6)/
     &	(eps1+eps2+eps3+eps4+eps5+eps6+debmap(i0,j0,k0))
	    end do
	  end do
	end do
	return
	end
