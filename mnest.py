# Licensed under a 3-clause BSD style license - see LICENSE
"""Simple implementation of nested sampling routine to evaluate Bayesian
evidence."""

from __future__ import division

import math
import time
from sys import stdout

import numpy as np
from scipy.cluster.vq import kmeans2

def choice(p):
    """replacement for numpy.random.choice (only in numpy 1.7+)"""

    r = np.random.random() * sum(p)
    i = 0
    t = p[i]
    while t < r:
        i += 1
        t += p[i]
        
class Ellipsoid(object):
    def __init__(self, ctr, cov, icov, vol):
        self.ctr = ctr    # center coordinates
        self.cov = cov    # covariance
        self.icov = icov  # cov^-1
        self.vol = vol    # volume

    def scale_to_vol(self, vol):
        ndim = len(self.ctr)
        factor = (vol / self.vol) ** (1./ndim)
        self.cov *= factor
        self.icov /= factor
        self.vol = vol

def bounding_ellipsoid(x):
    """
    Calculate bounding ellipsoid containing all samples x.

    Parameters
    ----------
    x : (nobj, ndim) ndarray
        Coordinates of points.

    Returns
    -------
    ellipsoid : Ellipsoid
        Attributes are:
        * ``ctr`` ndarray of shape (ndim,)

        * ``cov`` ndarray of shape (ndim, ndim)
          (f * C) which is the covariance of the data points, C,
          times an enlargement factor, f, that ensures that the ellipse
          defined by ``x^T <dot> (fC)^{-1} <dot> x <= 1`` encloses
          all points in the input set.

        * ``icov`` Inverse of cov.
        * ``vol`` Ellipse volume.
    """

    ctr = np.mean(x, axis=0)
    delta = x - ctr
    cov = np.cov(delta, rowvar=0)
    icov = np.linalg.inv(cov)

    # calculate expansion factor necessary to bound all the points
    f = np.empty(len(x), dtype=np.float)
    for i in range(len(x)):
        f[i] = np.dot(np.dot(delta[i,:], icov), delta[i,:])
    fmax = np.max(f)

    cov = fmax * cov
    icov = icov / fmax
    vol = ellipsoid_volume(cov)

    return Ellipsoid(ctr, cov, icov, vol)

def vol_factor(ndim):
    """
    if n is even:

    (2pi)^(n/2) / (2 * 4 * ... * n)

    if n is odd:

    2 * (2pi)^((n-1)/2) / (1 * 3 * ... * n)
    """

    if ndim % 2 == 0:
        f = 1.
        i = 2
    else:
        f = 2.
        i = 3

    while i <= ndim:
        f *= (2. / i * np.pi)
        i += 2
    return f

def spheroid_volume(ndim, r):
    return vol_factor(ndim) * r**ndim

def spheroid_radius(ndim, v):
    return (v / vol_factor(ndim))**(1./ndim)

def ellipsoid_volume(scaled_cov):
    """
    Parameters
    ----------
    scaled_cov : (ndim, ndim) ndarray
        Scaled covariance matrix.

    Returns
    -------
    volume : float
    """
    vol = np.sqrt(np.linalg.det(scaled_cov))

    # proportionality constant depending on dimension
    ndim = len(scaled_cov)
    if ndim % 2 == 0:
        i = 2
        while i <= ndim:
            vol *= (2. / i * np.pi)
            i += 2
    else:
        vol *= 2.
        i = 3
        while i <= ndim:
            vol *= (2. / i * np.pi)
            i += 2

    return vol

def bounding_ellipsoids(x, min_vol=None, ellipsoid=None):
    """Calculate a set of ellipses that bound the points.

    Parameters
    ----------
    x : (nobj, ndim) ndarray
        Coordinates of points.
    min_vol : float
        Minimum allowed volume of ellipses enclosing points.
    ellipsoid : (float, float), optional
        If known, the bounding ellipsoid of the points `x`. If not supplied,
        it will be calculated. This option is used when the function is
        called recursively.
    ellipsoid_vol : float, optional
        Volume of ellipsoid, if ellipsoid is not None.

    Returns
    -------
    ellipsoids : list of 2-tuples
        Ellipsoids, each represented by a tuple: ``(scaled_cov, x_mean)``
    """

    ellipsoids = []
    nobj, ndim = x.shape   

    # If we don't already have a bounding ellipse for the points,
    # calculate it, and enlarge it so that it has at least the minimum
    # volume.
    if ellipsoid is None:
        ellipsoid = bounding_ellipsoid(x) 
        if min_vol is not None and ellipsoid.vol < min_vol:
            ellipsoid.scale_to_vol(min_vol)

    # Split points into two clusters using k-means clustering with k=2
    # centroid = (2, ndim) ; label = (nobj,)
    # [Each entry in `label` is 0 or 1, corresponding to cluster number]
    centroid, label = kmeans2(x, 2, iter=10)

    cluster_x = [None, None]
    cluster_ellipsoids = [None, None]
    cluster_expectvols = [None, None]
    recalculate = True
    while recalculate:
        h = []
        for k in [0, 1]:            
            x_k = x[label == k, :] # points in this cluster
            ellipsoid_k = bounding_ellipsoid(x_k)

            # enlarge ellipse so that it is at least as large as the fractional
            # volume according to the number of points in the cluster
            if min_vol is not None:
                min_vol_k = min_vol * float(len(x_k)) / float(nobj)
                if ellipsoid_k.vol < min_vol_k:
                    ellipsoid_k.scale_to_vol(min_vol_k)
            else:
                min_vol_k = None

            # Calculate mahalanobis distance between ALL points and the
            # current cluster. The mahalanobis distance squared is given by:
            #     delta = u - v
            #     m = np.dot(np.dot(delta, VI), delta)
            # where, in this case,
            #     VI = (f * C)^-1 = (scaled_cov)^-1
            d = np.empty(len(x), dtype=np.float)
            delta = x - ellipsoid_k.ctr
            for i in range(len(x)):
                d[i] = np.dot(np.dot(delta[i,:], ellipsoid_k.icov), delta[i,:])

            # Multiply by ellipse ratio:
            # h_k(point) = V_k(actual) / V_k(expected) * d_k(point)
            # TODO: d is M. distance *squared*. Should it not be squared?
            if min_vol_k is not None:
                h.append((ellipsoid_k.vol / min_vol_k) * d)
            else:
                h.append(d)

            # Save cluster info, in case we exit on this iteration
            cluster_x[k] = x_k
            cluster_ellipsoids[k] = ellipsoid_k
            cluster_expectvols[k] = min_vol_k

        # reassign each point to the cluster that gives it the smallest h.
        # Here, we are creating a bool array, h[1] < h[0]
        #     True -> h smaller for #1 -> assign to cluster 1
        #     False -> h smaller for #0 -> assign to cluster 0
        # then the cast to int converts True->1, False->0
        newlabel = (h[1] < h[0]).astype(np.int)

        # If no points were reassigned, exit the loop.
        # Otherwise, update the assignment of points and continue looping.
        if np.all(newlabel == label):
            recalculate = False
        else:
            label = newlabel
            
    # if V(E_1) + V(E_2) < V(E) or V(E) > 2V(S):
    # perform entire algorithm on each subset
    if (cluster_ellipsoids[0].vol+cluster_ellipsoids[1].vol < ellipsoid.vol or
        (min_vol is not None and ellipsoid.vol > 2. * min_vol)):
        for k in [0, 1]:
            ellipsoids.extend(
                bounding_ellipsoids(cluster_x[k],
                                    min_vol=cluster_expectvols[k],
                                    ellipsoid=cluster_ellipsoids[k]))

    # Otherwise, the full ellipse is fine; just return that.
    else:
        ellipsoids.append(ellipsoid)

    return ellipsoids

def randsphere(n):
    """Draw a random point within a n-dimensional unit sphere"""

    z = np.random.randn(n)
    return z * np.random.rand()**(1./n) / np.sqrt(np.sum(z**2))

def sample_ellipsoid(ellipsoid, nsamples=1):
    """Chose sample(s) randomly distributed within an ellipsoid.
    
    Parameters
    ----------
    scaled_cov : (ndim, ndim) ndarray
        Scaled covariance matrix.
    x_mean : (ndim,) ndarray
        Simple average of all samples.

    Returns
    -------
    x : (nsamples, ndim) array, or (ndim,) array when nsamples == 1
        Coordinates within the ellipsoid.
    """

    # Get scaled eigenvectors (in columns): vs[:,i] is the i-th eigenvector.
    w, v = np.linalg.eig(ellipsoid.cov)
    vs = np.dot(v, np.diag(np.sqrt(w)))

    ndim = len(ellipsoid.ctr)
    if nsamples == 1:
        return np.dot(vs, randsphere(ndim)) + ellipsoid.ctr

    x = np.empty((nsamples, ndim), dtype=np.float)
    for i in range(nsamples):
        x[i, :] = np.dot(vs, randsphere(ndim)) + ellipsoid.ctr
    return x

def sample_ellipsoids(ellipsoids, nsamples=1):
    """Chose sample(s) randomly distributed within a set of
    (possibly overlapping) ellipsoids.
    
    Parameters
    ----------
    ellipsoids : list of 2-tuples
        Ellipsoids, each represented by a tuple: ``(scaled_cov, x_mean)``

    Returns
    -------
    x : numpy.ndarray (nsamples, ndim) [or (ndim,) when nsamples == 1]
        Coordinates within the ellipsoid. 
    """

    # Select an ellipsoid at random, according to volumes
    v = np.array([e.vol for e in ellipsoids])
    i = choice(v)
    ellipsoid = ellipsoids[i]
    
    # Select a point from the ellipsoid
    x = sample_ellipsoid(ellipsoid)

    # How many ellipsoids is the sample in?
    n = 0
    for ellipsoid in ellipsoids:
        delta = x - ellipsoid.ctr
        n += np.dot(np.dot(delta, ellipsoid.icov), delta) < 1.

    # Only accept the point with probability 1/n 
    if n > 1 and np.random.random() > 1./n:
        return sample_ellipsoids(ellipsoids)
    
    return x

def mnest(loglikelihood, prior, npar, nobj=50, maxiter=10000,
          verbose=False, verbose_name='', enlarge=1.):
    """Simple nested sampling algorithm to evaluate Bayesian evidence.

    Parameters
    ----------
    loglikelihood : func
        Function returning log(likelihood) given parameters as a 1-d numpy
        array of length `npar`. 
    prior : func
        Function translating a unit cube to the parameter space according to 
        the prior. The input is a 1-d numpy array with length `npar`, where
        each value is in the range [0, 1). The return value should also be a
        1-d numpy array with length `npar`, where each value is a parameter.
        The return value is passed to the loglikelihood function. For example,
        for a 2 parameter model with flat priors in the range [0, 2), the
        function would be

            def prior(u):
                return 2. * u

    npar : int
        Number of parameters.
    nobj : int, optional
        Number of random samples. Larger numbers result in a more finely
        sampled posterior (more accurate evidence), but also a larger
        number of iterations required to converge. Default is 50.
    maxiter : int, optional
        Maximum number of iterations. Iteration may stop earlier if
        termination condition is reached. Default is 10000. The total number
        of likelihood evaluations will be ``nexplore * niter``.
    verbose : bool, optional
        Print a single line of running total iterations.
    verbose_name : str, optional
        Print this string at start of the iteration line printed when
        verbose=True.

    Returns
    -------
    results : dict
        Containing following keys:

        * `niter` (int) number of iterations.
        * `ncalls` (int) number of likelihood calls.
        * `time` (float) time in seconds.
        * `logz` (float) log of evidence.
        * `logzerr` (float) error on `logz`.
        * `loglmax` (float) Maximum likelihood of any sample.
        * `h` (float) information.
        * `samples_parvals` (array, shape=(nsamples, npar)) parameter values
          of each sample.
        * `samples_wt` (array, shape=(nsamples,) Weight of each sample.

    Notes
    -----
    This is an implementation of John Skilling's Nested Sampling algorithm,
    following the ellipsoidal sampling algorithm in Shaw et al (2007). Only a
    single ellipsoid is used.
    
    Sample Weights are ``likelihood * prior_vol`` where
    prior_vol is the fraction of the prior volume the sample represents.

    References
    ----------
    http://www.inference.phy.cam.ac.uk/bayesys/
    Shaw, Bridges, Hobson 2007, MNRAS, 378, 1365
    """

    # Initialize objects and calculate likelihoods
    objects_u = np.random.random((nobj, npar)) #position in unit cube
    objects_v = np.empty((nobj, npar), dtype=np.float) #position in unit cube
    objects_logl = np.empty(nobj, dtype=np.float)  # log likelihood
    for i in range(nobj):
        objects_v[i,:] = prior(objects_u[i,:])
        objects_logl[i] = loglikelihood(objects_v[i,:])

    # Initialize values for nested sampling loop.
    samples_parvals = [] # stored objects for posterior results
    samples_logwt = []
    loglstar = None  # ln(Likelihood constraint)
    h = 0.  # Information, initially 0.
    logz = -1.e300  # ln(Evidence Z, initially 0)
    # ln(width in prior mass), outermost width is 1 - e^(-1/n)
    logwidth = math.log(1. - math.exp(-1./nobj))
    loglcalls = nobj #number of calls we already made

    # Nested sampling loop.
    ndecl = 0
    logwt_old = None
    time0 = time.time()
    for it in range(maxiter):
        if verbose:
            if logz > -1.e6:
                print "\r{} iter={:6d} logz={:8f}".format(verbose_name, it,
                                                          logz),
            else:
                print "\r{} iter={:6d} logz=".format(verbose_name, it),
            stdout.flush()

        # worst object in collection and its weight (= width * likelihood)
        worst = np.argmin(objects_logl)
        logwt = logwidth + objects_logl[worst]

        # update evidence Z and information h.
        logz_new = np.logaddexp(logz, logwt)
        h = (math.exp(logwt - logz_new) * objects_logl[worst] +
             math.exp(logz - logz_new) * (h + logz) -
             logz_new)
        logz = logz_new

        # Add worst object to samples.
        samples_parvals.append(np.array(objects_v[worst]))
        samples_logwt.append(logwt)

        # The new likelihood constraint is that of the worst object.
        loglstar = objects_logl[worst]

        # Bounding ellipsoid of all samples (including worst one)
        ellipsoids = bounding_ellipsoids(objects_u, math.exp(-i / nobj)*enlarge)

        # choose a point from within the ellipse until it has likelihood
        # better than loglstar
        while True:
            u = sample_ellipsoids(ellipsoids)
            if np.any(u < 0.) or np.any(u > 1.):
                continue
            v = prior(u)
            logl = loglikelihood(v)
            loglcalls += 1

            # Accept if and only if within likelihood constraint.
            if logl > loglstar:
                objects_u[worst] = u
                objects_v[worst] = v
                objects_logl[worst] = logl
                break

        # Shrink interval
        logwidth -= 1./nobj

        # stop when the logwt has been declining for more than 10 or niter/4
        # consecutive iterations.
        if logwt < logwt_old:
            ndecl += 1
        else:
            ndecl = 0
        if ndecl > 10 and ndecl > it / 6:
            break
        logwt_old = logwt

    tottime = time.time() - time0
    if verbose:
        print 'calls={:d} time={:7.3f}s'.format(loglcalls, tottime)

    # Add remaining objects.
    # After N samples have been taken out, the remaining width is e^(-N/nobj)
    # The remaining width for each object is e^(-N/nobj) / nobj
    # The log of this for each object is:
    # log(e^(-N/nobj) / nobj) = -N/nobj - log(nobj)
    logwidth = -len(samples_parvals) / nobj - math.log(nobj)
    for i in range(nobj):
        logwt = logwidth + objects_logl[i]
        logz_new = np.logaddexp(logz, logwt)
        h = (math.exp(logwt - logz_new) * objects_logl[i] +
             math.exp(logz - logz_new) * (h + logz) -
             logz_new)
        logz = logz_new
        samples_parvals.append(np.array(objects_v[i]))
        samples_logwt.append(logwt)

    return {
        'niter': it + 1,
        'ncalls': loglcalls,
        'time': tottime,
        'logz': logz,
        'logzerr': math.sqrt(h / nobj),
        'loglmax': np.max(objects_logl),
        'h': h,
        'samples_parvals': np.array(samples_parvals),  #(nsamp, npar)
        'samples_wt':  np.exp(np.array(samples_logwt) - logz)  #(nsamp,)
        }
