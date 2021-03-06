from __future__ import (absolute_import, division, print_function,
                        unicode_literals)
import six
import warnings

import numpy as np
import pandas as pd
from scipy import ndimage
from scipy.spatial import cKDTree
from pandas import DataFrame

from . import uncertainty
from .preprocessing import bandpass, scale_to_gamut
from .utils import record_meta, print_update, validate_tuple
from .masks import binary_mask, r_squared_mask, cosmask, sinmask
import trackpy  # to get trackpy.__version__

from .try_numba import try_numba_autojit, NUMBA_AVAILABLE


def percentile_threshold(image, percentile):
    """Find grayscale threshold based on distribution in image."""

    not_black = image[np.nonzero(image)]
    if len(not_black) == 0:
        return np.nan
    return np.percentile(not_black, percentile)


def local_maxima(image, radius, percentile=64, margin=None):
    """Find local maxima whose brightness is above a given percentile.

    Parameters
    ----------
    radius : integer definition of "local" in "local maxima"
    percentile : chooses minimum grayscale value for a local maximum
    margin : zone of exclusion at edges of image. Defaults to radius.
            A smarter value is set by locate().
    """
    if margin is None:
        margin = radius

    ndim = image.ndim
    # Compute a threshold based on percentile.
    threshold = percentile_threshold(image, percentile)
    if np.isnan(threshold):
        warnings.warn("Image is completely black.", UserWarning)
        return np.empty((0, ndim))

    # The intersection of the image with its dilation gives local maxima.
    if not np.issubdtype(image.dtype, np.integer):
        raise TypeError("Perform dilation on exact (i.e., integer) data.")
    footprint = binary_mask(radius, ndim)
    dilation = ndimage.grey_dilation(image, footprint=footprint,
                                     mode='constant')
    maxima = np.vstack(np.where((image == dilation) & (image > threshold))).T
    if not np.size(maxima) > 0:
        warnings.warn("Image contains no local maxima.", UserWarning)
        return np.empty((0, ndim))

    # Do not accept peaks near the edges.
    shape = np.array(image.shape)
    near_edge = np.any((maxima < margin) | (maxima > (shape - margin - 1)), 1)
    maxima = maxima[~near_edge]
    if not np.size(maxima) > 0:
        warnings.warn("All local maxima were in the margins.", UserWarning)

    # Return coords in as a numpy array shaped so it can be passed directly
    # to the DataFrame constructor.
    return maxima


def estimate_mass(image, radius, coord):
    "Compute the total brightness in the neighborhood of a local maximum."
    square = [slice(c - rad, c + rad + 1) for c, rad in zip(coord, radius)]
    neighborhood = binary_mask(radius, image.ndim)*image[square]
    return np.sum(neighborhood)


def estimate_size(image, radius, coord, estimated_mass):
    "Compute the total brightness in the neighborhood of a local maximum."
    square = [slice(c - rad, c + rad + 1) for c, rad in zip(coord, radius)]
    neighborhood = binary_mask(radius, image.ndim)*image[square]
    Rg = np.sqrt(np.sum(r_squared_mask(radius, image.ndim) * neighborhood) /
                 estimated_mass)
    return Rg


def _safe_center_of_mass(x, radius, grids):
    normalizer = x.sum()
    if normalizer == 0:  # avoid divide-by-zero errors
        return np.array(radius)
    return np.array([(x * grids[dim]).sum() / normalizer
                    for dim in range(x.ndim)])


def refine(raw_image, image, radius, coords, separation=0, max_iterations=10,
           engine='auto', characterize=True, walkthrough=False):
    """Find the center of mass of a bright feature starting from an estimate.

    Characterize the neighborhood of a local maximum, and iteratively
    hone in on its center-of-brightness. Return its coordinates, integrated
    brightness, size (Rg), eccentricity (0=circular), and signal strength.

    Parameters
    ----------
    raw_image : array (any dimensions)
        used for final characterization
    image : array (any dimension)
        processed image, used for locating center of mass
    coord : array
        estimated position
    max_iterations : integer
        max number of loops to refine the center of mass, default 10
    characterize : boolean, True by default
        Compute and return mass, size, eccentricity, signal.
    walkthrough : boolean, False by default
        Print the offset on each loop and display final neighborhood image.
    engine : {'python', 'numba'}
        Numba is faster if available, but it cannot do walkthrough.
    """
    # ensure that radius is tuple of integers, for direct calls to refine()
    radius = validate_tuple(radius, image.ndim)
    # Main loop will be performed in separate function.
    if engine == 'auto':
        if NUMBA_AVAILABLE:
            engine = 'numba'
        else:
            engine = 'python'
    if engine == 'python':
        coords = np.array(coords)  # a copy, will not modify in place
        results = _refine(raw_image, image, radius, coords, max_iterations,
                          characterize, walkthrough)
    elif engine == 'numba':
        if not NUMBA_AVAILABLE:
            warnings.warn("numba could not be imported. Without it, the "
                          "'numba' engine runs very slow. Use the 'python' "
                          "engine or install numba.", UserWarning)
        if image.ndim != 2:
            raise NotImplementedError("The numba engine only supports 2D "
                                      "images. You can extend it if you feel "
                                      "like a hero.")
        if radius[0] != radius[1]:
            raise NotImplementedError("The numba engine does not support "
                                      "anisotropic feature finding. "
                                      "You can extend it if you feel "
                                      "like a hero.")
        if walkthrough:
            raise ValueError("walkthrough is not availabe in the numba engine")
        # Do some extra prep in pure Python that can't be done in numba.
        coords = np.array(coords, dtype=np.float64)
        N = coords.shape[0]
        shape = np.array(image.shape, dtype=np.int16)  # array, not tuple
        mask = binary_mask(radius[0], image.ndim)
        r2_mask = r_squared_mask(radius[0], image.ndim)
        cmask = cosmask(radius[0])
        smask = sinmask(radius[0])
        # Allocate a results array that _numba_refine will write to
        if characterize:
            results_columns = 6
        else:
            results_columns = 3  # Position and mass only
        results = np.empty((N, results_columns), dtype=np.float64)
        _ = _numba_refine(np.asarray(raw_image), np.asarray(image), int(radius[0]),
                                coords, N,
                                int(max_iterations), characterize,
                                shape, mask, r2_mask, cmask, smask, results,
                                np.empty(2, dtype=np.float64),  # Buffer arrays.
                                np.empty(2, dtype=np.float64),  # See function def.
                                np.empty(2, dtype=np.float64),
                                np.empty(2, dtype=np.float64),
                                np.empty(2, dtype=np.int64),)
    else:
        raise ValueError("Available engines are 'python' and 'numba'")

    # Flat peaks return multiple nearby maxima. Eliminate duplicates.
    if np.all(np.greater(separation, 0)):
        mass_index = image.ndim  # i.e., index of the 'mass' column
        while True:
            # Rescale positions, so that pairs are identified below a distance
            # of 1. Do so every iteration (room for improvement?)
            positions = results[:, :mass_index]/list(reversed(separation))
            mass = results[:, mass_index]
            duplicates = cKDTree(positions, 30).query_pairs(1)
            if len(duplicates) == 0:
                break
            to_drop = []
            for pair in duplicates:
                # Drop the dimmer one.
                if np.equal(*mass.take(pair, 0)):
                    # Rare corner case: a tie!
                    # Break ties by sorting by sum of coordinates, to avoid
                    # any randomness resulting from cKDTree returning a set.
                    dimmer = np.argsort(np.sum(positions.take(pair, 0), 1))[0]
                else:
                    dimmer = np.argmin(mass.take(pair, 0))
                to_drop.append(pair[dimmer])
            results = np.delete(results, to_drop, 0)

    return results


# (This is pure Python. A numba variant follows below.)
def _refine(raw_image, image, radius, coords, max_iterations,
            characterize, walkthrough):
    SHIFT_THRESH = 0.6
    GOOD_ENOUGH_THRESH = 0.005

    ndim = image.ndim
    mask = binary_mask(radius, ndim)
    slices = [[slice(c - rad, c + rad + 1) for c, rad in zip(coord, radius)]
              for coord in coords]

    # Declare arrays that we will fill iteratively through loop.
    N = coords.shape[0]
    final_coords = np.empty_like(coords, dtype=np.float64)
    mass = np.empty(N, dtype=np.float64)
    Rg = np.empty(N, dtype=np.float64)
    ecc = np.empty(N, dtype=np.float64)
    signal = np.empty(N, dtype=np.float64)
    ogrid = np.ogrid[[slice(0, i) for i in mask.shape]]  # for center of mass
    ogrid = [g.astype(float) for g in ogrid]

    for feat in range(N):
        coord = coords[feat]

        # Define the circular neighborhood of (x, y).
        rect = slices[feat]
        neighborhood = mask*image[rect]
        cm_n = _safe_center_of_mass(neighborhood, radius, ogrid)
        cm_i = cm_n - radius + coord  # image coords
        allow_moves = True
        for iteration in range(max_iterations):
            off_center = cm_n - radius
            if walkthrough:
                print_update(off_center)
            if np.all(np.abs(off_center) < GOOD_ENOUGH_THRESH):
                break  # Accurate enough.

            # If we're off by more than half a pixel in any direction, move.
            elif np.any(np.abs(off_center) > SHIFT_THRESH) & allow_moves:
                # In here, coord is an integer.
                new_coord = coord
                new_coord[off_center > SHIFT_THRESH] += 1
                new_coord[off_center < -SHIFT_THRESH] -= 1
                # Don't move outside the image!
                upper_bound = np.array(image.shape) - 1 - radius
                new_coord = np.clip(new_coord, radius, upper_bound).astype(int)
                # Update slice to shifted position.
                rect = [slice(c - rad, c + rad + 1)
                        for c, rad in zip(new_coord, radius)]
                neighborhood = mask*image[rect]

            # If we're off by less than half a pixel, interpolate.
            else:
                # Here, coord is a float. We are off the grid.
                neighborhood = ndimage.shift(neighborhood, -off_center,
                                             order=2, mode='constant', cval=0)
                new_coord = coord + off_center
                # Disallow any whole-pixels moves on future iterations.
                allow_moves = False

            cm_n = _safe_center_of_mass(neighborhood, radius, ogrid)  # neighborhood
            cm_i = cm_n - radius + new_coord  # image coords
            coord = new_coord
        # matplotlib and ndimage have opposite conventions for xy <-> yx.
        final_coords[feat] = cm_i[..., ::-1]

        if walkthrough:
            import matplotlib.pyplot as plt
            plt.imshow(neighborhood)

        # Characterize the neighborhood of our final centroid.
        mass[feat] = neighborhood.sum()
        if not characterize:
            continue  # short-circuit loop
        Rg[feat] = np.sqrt(np.sum(r_squared_mask(radius, ndim) *
                                  neighborhood) / mass[feat])
        # I only know how to measure eccentricity in 2D.
        if ndim == 2 and radius[0] == radius[1]:
            rad = radius[0]
            ecc[feat] = np.sqrt(np.sum(neighborhood*cosmask(rad))**2 +
                                np.sum(neighborhood*sinmask(rad))**2)
            ecc[feat] /= (mass[feat] - neighborhood[rad, rad] + 1e-6)
        else:
            ecc[feat] = np.nan
        raw_neighborhood = mask*raw_image[rect]
        signal[feat] = raw_neighborhood.max()  # black_level subtracted later

    if not characterize:
        result = np.column_stack([final_coords, mass])
    else:
        result = np.column_stack([final_coords, mass, Rg, ecc, signal])
    return result


@try_numba_autojit(nopython=False)
def _numba_refine(raw_image, image, radius, coords, N, max_iterations,
                  characterize, shape, mask, r2_mask, cmask, smask, results,
                  coord, cm_n, cm_i, off_center, new_coord):
    SHIFT_THRESH = 0.6
    GOOD_ENOUGH_THRESH = 0.01
    # Column indices into the 'results' array
    MASS_COL = 2
    RG_COL = 3
    ECC_COL = 4
    SIGNAL_COL = 5

    square_size = 2*radius + 1

    for feat in range(N):
        # Define the circular neighborhood of (x, y).
        for dim in range(2):
            coord[dim] = coords[feat, dim]
            cm_n[dim] = 0.
        square0 = int(round(coord[0])) - radius
        square1 = int(round(coord[1])) - radius
        mass_ = 0.0
        for i in range(square_size):
            for j in range(square_size):
                if mask[i, j] != 0:
                    px = image[square0 + i, square1 + j]
                    cm_n[0] += px*i
                    cm_n[1] += px*j
                    mass_ += px

        for dim in range(2):
            cm_n[dim] /= mass_
            cm_i[dim] = cm_n[dim] - radius + coord[dim]
        allow_moves = True
        for iteration in range(max_iterations):
            for dim in range(2):
                off_center[dim] = cm_n[dim] - radius
            for dim in range(2):
                if abs(off_center[dim]) > GOOD_ENOUGH_THRESH:
                    break  # Proceed through iteration.
            else:
                break

            # If we're off by more than half a pixel in any direction, move.
            do_move = False
            if allow_moves:
                for dim in range(2):
                    if abs(off_center[dim]) > SHIFT_THRESH:
                        do_move = True
                        break

            if do_move:
                # In here, coord is an integer.
                for dim in range(2):
                    new_coord[dim] = int(round(coord[dim]))
                    oc = off_center[dim]
                    if oc > SHIFT_THRESH:
                        new_coord[dim] += 1
                    elif oc < - SHIFT_THRESH:
                        new_coord[dim] += -1
                    # Don't move outside the image!
                    if new_coord[dim] < radius:
                        new_coord[dim] = radius
                    upper_bound = shape[dim] - radius - 1
                    if new_coord[dim] > upper_bound:
                        new_coord[dim] = upper_bound
                # Update slice to shifted position.
                square0 = new_coord[0] - radius
                square1 = new_coord[1] - radius
                for dim in range(2):
                    cm_n[dim] = 0.

            # If we're off by less than half a pixel, interpolate.
            else:
                break
                # TODO Implement this for numba.
                # Remember to zero cm_n somewhere in here.
                # Here, coord is a float. We are off the grid.
                # neighborhood = ndimage.shift(neighborhood, -off_center,
                #                              order=2, mode='constant', cval=0)
                # new_coord = np.float_(coord) + off_center
                # Disallow any whole-pixels moves on future iterations.
                # allow_moves = False

            # cm_n was re-zeroed above in an unrelated loop
            mass_ = 0.
            for i in range(square_size):
                for j in range(square_size):
                    if mask[i, j] != 0:
                        px = image[square0 + i, square1 + j]
                        cm_n[0] += px*i
                        cm_n[1] += px*j
                        mass_ += px

            for dim in range(2):
                cm_n[dim] /= mass_
                cm_i[dim] = cm_n[dim] - radius + new_coord[dim]
                coord[dim] = new_coord[dim]
        # matplotlib and ndimage have opposite conventions for xy <-> yx.
        results[feat, 0] = cm_i[1]
        results[feat, 1] = cm_i[0]

        # Characterize the neighborhood of our final centroid.
        mass_ = 0.
        Rg_ = 0.
        ecc1 = 0.
        ecc2 = 0.
        signal_ = 0.
        for i in range(square_size):
            for j in range(square_size):
                if mask[i, j] != 0:
                    px = image[square0 + i, square1 + j]
                    mass_ += px
                    # Will short-circuiting if characterize=False slow it down?
                    if not characterize:
                        continue
                    Rg_ += r2_mask[i, j]*px
                    ecc1 += cmask[i, j]*px
                    ecc2 += smask[i, j]*px
                    raw_px = raw_image[square0 + i, square1 + j]
                    if raw_px > signal_:
                        signal_ = px
        results[feat, MASS_COL] = mass_
        if characterize:
            results[feat, RG_COL] = np.sqrt(Rg_/mass_)
            center_px = image[square0 + radius, square1 + radius]
            results[feat, ECC_COL] = np.sqrt(ecc1**2 + ecc2**2)/(mass_ - center_px + 1e-6)
            results[feat, SIGNAL_COL] = signal_  # black_level subtracted later

    return 0  # Unused


def locate(raw_image, diameter, minmass=100., maxsize=None, separation=None,
           noise_size=1, smoothing_size=None, threshold=None, invert=False,
           percentile=64, topn=None, preprocess=True, max_iterations=10,
           filter_before=True, filter_after=True,
           characterize=True, engine='auto'):
    """Locate Gaussian-like blobs of some approximate size in an image.

    Preprocess the image by performing a band pass and a threshold.
    Locate all peaks of brightness, characterize the neighborhoods of the peaks
    and take only those with given total brightnesss ("mass"). Finally,
    refine the positions of each peak.

    Parameters
    ----------
    image : image array (any dimensions)
    diameter : feature size in px
        This may be a single number or a tuple giving the feature's
        extent in each dimension, useful when the dimensions do not have
        equal resolution (e.g. confocal microscopy). The tuple order is the
        same as the image shape, conventionally (z, y, x) or (y, x). The
        number(s) must be odd integers. When in doubt, round up.
    minmass : minimum integrated brightness
        Default is 100, but a good value is often much higher. This is a
        crucial parameter for elminating spurious features.
    maxsize : maximum radius-of-gyration of brightness, default None
    separation : feature separation, in pixels
        Default is diameter + 1. May be a tuple, see diameter for details.
    noise_size : width of Gaussian blurring kernel, in pixels
        Default is 1. May be a tuple, see diameter for details.
    smoothing_size : size of boxcar smoothing, in pixels
        Default is diameter. May be a tuple, see diameter for details.
    threshold : Clip bandpass result below this value.
        Default None, passed through to bandpass.
    invert : Set to True if features are darker than background. False by
        default.
    percentile : Features must have a peak brighter than pixels in this
        percentile. This helps eliminate spurious peaks.
    topn : Return only the N brightest features above minmass.
        If None (default), return all features above minmass.

    Returns
    -------
    DataFrame([x, y, mass, size, ecc, signal])
        where mass means total integrated brightness of the blob,
        size means the radius of gyration of its Gaussian-like profile,
        and ecc is its eccentricity (1 is circular).

    Other Parameters
    ----------------
    preprocess : Set to False to turn out bandpass preprocessing.
    max_iterations : integer
        max number of loops to refine the center of mass, default 10
    filter_before : boolean
        Use minmass (and maxsize, if set) to eliminate spurious features
        based on their estimated mass and size before refining position.
        True by default for performance.
    filter_after : boolean
        Use final characterizations of mass and size to eliminate spurious
        features. True by default.
    characterize : boolean
        Compute "extras": eccentricity, signal, ep. True by default.
    engine : {'auto', 'python', 'numba'}

    See Also
    --------
    batch : performs location on many images in batch

    Notes
    -----
    Locate works with a coordinate system that has its origin at the center of
    pixel (0, 0). In almost all cases this will be the topleft pixel: the
    y-axis is pointing downwards.

    This is an implementation of the Crocker-Grier centroid-finding algorithm.
    [1]_

    References
    ----------
    .. [1] Crocker, J.C., Grier, D.G. http://dx.doi.org/10.1006/jcis.1996.0217

    """

    # Validate parameters and set defaults.
    raw_image = np.squeeze(raw_image)
    shape = raw_image.shape
    ndim = len(shape)

    diameter = validate_tuple(diameter, ndim)
    diameter = tuple([int(x) for x in diameter])
    if not np.all([x & 1 for x in diameter]):
        raise ValueError("Feature diameter must be an odd integer. Round up.")
    radius = tuple([x//2 for x in diameter])

    if separation is None:
        separation = tuple([x + 1 for x in diameter])
    else:
        separation = validate_tuple(separation, ndim)

    if smoothing_size is None:
        smoothing_size = diameter
    else:
        smoothing_size = validate_tuple(smoothing_size, ndim)

    noise_size = validate_tuple(noise_size, ndim)

    # Don't do characterization for rectangular pixels/voxels
    if diameter[1:] != diameter[:-1]:
        characterize = False

    # Check whether the image looks suspiciously like a color image.
    if 3 in shape or 4 in shape:
        dim = raw_image.ndim
        warnings.warn("I am interpreting the image as {0}-dimensional. "
                      "If it is actually a {1}-dimensional color image, "
                      "convert it to grayscale first.".format(dim, dim-1))
    if preprocess:
        if invert:
            # It is tempting to do this in place, but if it is called multiple
            # times on the same image, chaos reigns.
            if np.issubdtype(raw_image.dtype, np.integer):
                max_value = np.iinfo(raw_image.dtype).max
                raw_image = raw_image ^ max_value
            else:
                # To avoid degrading performance, assume gamut is zero to one.
                # Have you ever encountered an image of unnormalized floats?
                raw_image = 1 - raw_image
        image = bandpass(raw_image, noise_size, smoothing_size, threshold)
    else:
        image = raw_image.copy()
    # Coerce the image into integer type. Rescale to fill dynamic range.
    if np.issubdtype(raw_image.dtype, np.integer):
        dtype = raw_image.dtype
    else:
        dtype = np.uint8
    image = scale_to_gamut(image, dtype)

    # Set up a DataFrame for the final results.
    if image.ndim < 4:
        coord_columns = ['x', 'y', 'z'][:image.ndim]
    else:
        coord_columns = map(lambda i: 'x' + str(i), range(image.ndim))
    char_columns = ['mass']
    if characterize:
        char_columns += ['size', 'ecc', 'signal']
    columns = coord_columns + char_columns
    # The 'ep' column is joined on at the end, so we need this...
    if characterize:
        all_columns = columns + ['ep']
    else:
        all_columns = columns

    # Find local maxima.
    # Define zone of exclusion at edges of image, avoiding
    #   - Features with incomplete image data ("radius")
    #   - Extended particles that cannot be explored during subpixel
    #       refinement ("separation")
    #   - Invalid output of the bandpass step ("smoothing_size")
    margin = tuple([max(rad, sep // 2 - 1, sm // 2) for (rad, sep, sm) in
                    zip(radius, separation, smoothing_size)])
    coords = local_maxima(image, radius, percentile, margin)
    count_maxima = coords.shape[0]

    if count_maxima == 0:
        return DataFrame(columns=all_columns)

    # Proactively filter based on estimated mass/size before
    # refining positions.
    if filter_before:
        approx_mass = np.empty(count_maxima)  # initialize to avoid appending
        for i in range(count_maxima):
            approx_mass[i] = estimate_mass(image, radius, coords[i])
        condition = approx_mass > minmass
        if maxsize is not None:
            approx_size = np.empty(count_maxima)
            for i in range(count_maxima):
                approx_size[i] = estimate_size(image, radius, coords[i],
                                               approx_mass[i])
            condition &= approx_size < maxsize
        coords = coords[condition]
    count_qualified = coords.shape[0]

    if count_qualified == 0:
        warnings.warn("No maxima survived mass- and size-based prefiltering.")
        return DataFrame(columns=all_columns)

    # Refine their locations and characterize mass, size, etc.
    refined_coords = refine(raw_image, image, radius, coords, separation,
                            max_iterations, engine, characterize)

    # Filter again, using final ("exact") mass -- and size, if set.
    MASS_COLUMN_INDEX = image.ndim
    SIZE_COLUMN_INDEX = image.ndim + 1
    exact_mass = refined_coords[:, MASS_COLUMN_INDEX]
    if filter_after:
        condition = exact_mass > minmass
        if maxsize is not None:
            exact_size = refined_coords[:, SIZE_COLUMN_INDEX]
            condition &= exact_size < maxsize
        refined_coords = refined_coords[condition]
        exact_mass = exact_mass[condition]  # used below by topn
    count_qualified = refined_coords.shape[0]

    if count_qualified == 0:
        warnings.warn("No maxima survived mass- and size-based filtering.")
        return DataFrame(columns=all_columns)

    if topn is not None and count_qualified > topn:
        if topn == 1:
            # special case for high performance and correct shape
            refined_coords = refined_coords[np.argmax(exact_mass)]
            refined_coords = refined_coords.reshape(1, -1)
        else:
            refined_coords = refined_coords[np.argsort(exact_mass)][-topn:]

    f = DataFrame(refined_coords, columns=columns)

    # Estimate the uncertainty in position using signal (measured in refine)
    # and noise (measured here below).
    if characterize:
        black_level, noise = uncertainty.measure_noise(
            raw_image, diameter, threshold)
        f['signal'] -= black_level
        ep = uncertainty.static_error(f, noise, diameter[0], noise_size[0])
        f = f.join(ep)

    # If this is a pims Frame object, it has a frame number.
    # Tag it on; this is helpful for parallelization.
    if hasattr(raw_image, 'frame_no') and raw_image.frame_no is not None:
        f['frame'] = raw_image.frame_no
    return f


def batch(frames, diameter, minmass=100, maxsize=None, separation=None,
          noise_size=1, smoothing_size=None, threshold=None, invert=False,
          percentile=64, topn=None, preprocess=True, max_iterations=10,
          filter_before=True, filter_after=True,
          characterize=True, engine='auto',
          output=None, meta=True):
    """Locate Gaussian-like blobs of some approximate size in a set of images.

    Preprocess the image by performing a band pass and a threshold.
    Locate all peaks of brightness, characterize the neighborhoods of the peaks
    and take only those with given total brightnesss ("mass"). Finally,
    refine the positions of each peak.

    Parameters
    ----------
    frames : list (or iterable) of images
    diameter : feature size in px
        This may be a single number or a tuple giving the feature's
        extent in each dimension, useful when the dimensions do not have
        equal resolution (e.g. confocal microscopy). The tuple order is the
        same as the image shape, conventionally (z, y, x) or (y, x). The
        number(s) must be odd integers. When in doubt, round up.
    minmass : minimum integrated brightness
        Default is 100, but a good value is often much higher. This is a
        crucial parameter for elminating spurious features.
    maxsize : maximum radius-of-gyration of brightness, default None
    separation : feature separation, in pixels
        Default is diameter + 1. May be a tuple, see diameter for details.
    noise_size : width of Gaussian blurring kernel, in pixels
        Default is 1. May be a tuple, see diameter for details.
    smoothing_size : size of boxcar smoothing, in pixels
        Default is diameter. May be a tuple, see diameter for details.
    threshold : Clip bandpass result below this value.
        Default None, passed through to bandpass.
    invert : Set to True if features are darker than background. False by
        default.
    percentile : Features must have a peak brighter than pixels in this
        percentile. This helps eliminate spurious peaks.
    topn : Return only the N brightest features above minmass.
        If None (default), return all features above minmass.

    Returns
    -------
    DataFrame([x, y, mass, size, ecc, signal])
        where mass means total integrated brightness of the blob,
        size means the radius of gyration of its Gaussian-like profile,
        and ecc is its eccentricity (1 is circular).

    Other Parameters
    ----------------
    preprocess : Set to False to turn off bandpass preprocessing.
    max_iterations : integer
        max number of loops to refine the center of mass, default 10
    filter_before : boolean
        Use minmass (and maxsize, if set) to eliminate spurious features
        based on their estimated mass and size before refining position.
        True by default for performance.
    filter_after : boolean
        Use final characterizations of mass and size to elminate spurious
        features. True by default.
    characterize : boolean
        Compute "extras": eccentricity, signal, ep. True by default.
    engine : {'auto', 'python', 'numba'}
    output : {None, trackpy.PandasHDFStore, SomeCustomClass}
        If None, return all results as one big DataFrame. Otherwise, pass
        results from each frame, one at a time, to the write() method
        of whatever class is specified here.
    meta : By default, a YAML (plain text) log file is saved in the current
        directory. You can specify a different filepath set False.

    See Also
    --------
    locate : performs location on a single image

    Notes
    -----
    This is an implementation of the Crocker-Grier centroid-finding algorithm.
    [1]_

    Locate works with a coordinate system that has its origin at the center of
    pixel (0, 0). In almost all cases this will be the topleft pixel: the
    y-axis is pointing downwards.

    References
    ----------
    .. [1] Crocker, J.C., Grier, D.G. http://dx.doi.org/10.1006/jcis.1996.0217

    """
    # Gather meta information and save as YAML in current directory.
    timestamp = pd.datetime.utcnow().strftime('%Y-%m-%d-%H%M%S')
    try:
        source = frames.filename
    except:
        source = None
    meta_info = dict(timestamp=timestamp,
                     trackpy_version=trackpy.__version__,
                     source=source, diameter=diameter, minmass=minmass,
                     maxsize=maxsize, separation=separation,
                     noise_size=noise_size, smoothing_size=smoothing_size,
                     invert=invert, percentile=percentile, topn=topn,
                     preprocess=preprocess, max_iterations=max_iterations,
                     filter_before=filter_before, filter_after=filter_after)

    if meta:
        if isinstance(meta, str):
            filename = meta
        else:
            filename = 'feature_log_%s.yml' % timestamp
        record_meta(meta_info, filename)

    all_features = []
    for i, image in enumerate(frames):
        features = locate(image, diameter, minmass, maxsize, separation,
                          noise_size, smoothing_size, threshold, invert,
                          percentile, topn, preprocess, max_iterations,
                          filter_before, filter_after, characterize,
                          engine)
        if hasattr(image, 'frame_no') and image.frame_no is not None:
            frame_no = image.frame_no
            # If this works, locate created a 'frame' column.
        else:
            frame_no = i
            features['frame'] = i  # just counting iterations
        message = "Frame %d: %d features" % (frame_no, len(features))
        print_update(message)
        if len(features) == 0:
            continue

        if output is None:
            all_features.append(features)
        else:
            output.put(features)

    if output is None:
        return pd.concat(all_features).reset_index(drop=True)
    else:
        return output
