from __future__ import (absolute_import, division, print_function,
                        unicode_literals)
import six
from six.moves import range
import os
import unittest
import warnings

import nose
import numpy as np
import pandas as pd
from pandas import DataFrame, Series
from numpy.testing import assert_almost_equal, assert_allclose
from numpy.testing.decorators import slow
from pandas.util.testing import (assert_series_equal, assert_frame_equal,
                                 assert_produces_warning)

import trackpy as tp
from trackpy.try_numba import NUMBA_AVAILABLE
from trackpy.artificial import (draw_feature, draw_spots, draw_point,
                                gen_nonoverlapping_locations)


path, _ = os.path.split(os.path.abspath(__file__))


def compare(shape, count, radius, noise_level, engine):
    radius = tp.utils.validate_tuple(radius, len(shape))
    # tp.locate ignores a margin of size radius, take 1 px more to be safe
    margin = tuple([r + 1 for r in radius])
    diameter = tuple([(r * 2) + 1 for r in radius])
    draw_range = tuple([d * 3 for d in diameter])
    cols = ['x', 'y', 'z'][:len(shape)][::-1]
    pos = gen_nonoverlapping_locations(shape, count, draw_range, margin)
    image = draw_spots(shape, pos, draw_range, noise_level)
    f = tp.locate(image, diameter, engine=engine)
    actual = f[cols].sort(cols)
    expected = DataFrame(pos, columns=cols).sort(cols)
    return actual, expected


class CommonFeatureIdentificationTests(object):

    def check_skip(self):
        pass

    def skip_numba(self):
        pass

    def test_smoke_datatypes(self):
        self.check_skip()
        SHAPE = (300, 300)
        # simple "smoke" test to see if numba explodes
        dummy_image = np.random.randint(0, 100, SHAPE).astype(np.uint8)
        tp.locate(dummy_image, 5, engine=self.engine)
        tp.locate(dummy_image, 5, invert=True, engine=self.engine)

        # Check float types
        dummy_image = np.random.rand(*SHAPE)
        tp.locate(dummy_image, 5, engine=self.engine)
        tp.locate(dummy_image, 5, invert=True, engine=self.engine)

    def test_black_image(self):
        self.check_skip()
        black_image = np.zeros((21, 23)).astype(np.uint8)
        warnings.simplefilter('always')
        with assert_produces_warning(UserWarning):
            f = tp.locate(black_image, 5, engine=self.engine, preprocess=False)

    def test_maxima_in_margin(self):
        self.check_skip()
        black_image = np.ones((21, 23)).astype(np.uint8)
        draw_point(black_image, [1, 1], 100)
        with assert_produces_warning(UserWarning):
            f = tp.locate(black_image, 5, engine=self.engine)

    def test_maxima_in_margin_3D(self):
        self.skip_numba()
        black_image = np.ones((21, 23, 25)).astype(np.uint8)
        draw_point(black_image, [1, 1, 1], 100)
        with assert_produces_warning(UserWarning):
            f = tp.locate(black_image, 5, engine=self.engine)

    def test_all_maxima_filtered(self):
        self.check_skip()
        black_image = np.ones((21, 23)).astype(np.uint8)
        draw_point(black_image, [11, 13], 10)
        with assert_produces_warning(UserWarning):
            f = tp.locate(black_image, 5, minmass=1000,
                          engine=self.engine, preprocess=False)

    def test_warn_color_image(self):
        self.check_skip()

        # RGB-like
        image = np.random.randint(0, 100, (21, 23, 3)).astype(np.uint8)
        with assert_produces_warning(UserWarning):
            tp.locate(image, 5)

        # RGBA-like
        image = np.random.randint(0, 100, (21, 23, 4)).astype(np.uint8)
        with assert_produces_warning(UserWarning):
            tp.locate(image, 5)

    def test_flat_peak(self):
        # This tests the part of locate_maxima that eliminates multiple
        # maxima in the same mask area.
        self.check_skip()
        image = np.ones((21, 23)).astype(np.uint8)
        image[11, 13] = 100
        image[11, 14] = 100
        image[12, 13] = 100
        count = len(tp.locate(image, 5, preprocess=False,
                              engine=self.engine))
        self.assertEqual(count, 1)

        image = np.ones((21, 23)).astype(np.uint8)
        image[11:13, 13:15] = 100
        count = len(tp.locate(image, 5, preprocess=False,
                              engine=self.engine))
        self.assertEqual(count, 1)

        image = np.ones((21, 23)).astype(np.uint8)
        image[11, 13] = 100
        image[11, 14] = 100
        image[11, 15] = 100
        count = len(tp.locate(image, 5, preprocess=False,
                              engine=self.engine))
        self.assertEqual(count, 1)

        # This tests that two nearby peaks are merged by
        # picking the one with the brighter neighborhood.
        image = np.ones((21, 23)).astype(np.uint8)
        pos = [14, 14]

        draw_point(image, [11, 13], 100)
        draw_point(image, [11, 14], 100)
        draw_point(image, [11, 15], 100)
        draw_point(image, [14, 13], 101)
        draw_point(image, [14, 14], 101)
        draw_point(image, [14, 15], 101)
        cols = ['y', 'x']
        actual = tp.locate(image, 5, preprocess=False,
                           engine=self.engine)[cols]

        expected = DataFrame(np.asarray(pos).reshape(1, -1), columns=cols)
        assert_allclose(actual, expected, atol=0.1)

        # Break ties by sorting by position, simply to avoid
        # any randomness resulting from cKDTree returning a set.
        image = np.ones((21, 23)).astype(np.uint8)
        pos = [14, 14]

        draw_point(image, [11, 12], 100)
        draw_point(image, [11, 13], 100)
        draw_point(image, [11, 14], 100)
        draw_point(image, [14, 13], 100)
        draw_point(image, [14, 14], 100)
        draw_point(image, [14, 15], 100)
        cols = ['y', 'x']
        actual = tp.locate(image, 5, preprocess=False,
                           engine=self.engine)[cols]

        expected = DataFrame(np.asarray(pos).reshape(1, -1), columns=cols)
        assert_allclose(actual, expected, atol=0.1)

    def test_one_centered_gaussian(self):
        self.check_skip()
        L = 21
        dims = (L, L + 2)  # avoid square images in tests
        pos = [7, 13]
        cols = ['y', 'x']
        expected = DataFrame(np.asarray(pos).reshape(1, -1), columns=cols)

        image = np.ones(dims, dtype='uint8')
        draw_feature(image, pos, 27)
        actual = tp.locate(image, 9, 1, preprocess=False,
                           engine=self.engine)[cols]
        assert_allclose(actual, expected, atol=0.1)

    def test_one_centered_gaussian_3D(self):
        self.skip_numba()
        L = 21
        dims = (L, L + 2, L + 4)  # avoid square images in tests
        pos = [7, 13, 9]
        cols = ['z', 'y', 'x']
        expected = DataFrame(np.asarray(pos).reshape(1, -1), columns=cols)

        image = np.ones(dims, dtype='uint8')
        draw_feature(image, pos, 27)
        actual = tp.locate(image, 9, 1, preprocess=False,
                           engine=self.engine)[cols]
        assert_allclose(actual, expected, atol=0.1)

    def test_one_centered_gaussian_3D_anisotropic(self):
        self.skip_numba()
        L = 21
        dims = (L, L + 2, L + 4)  # avoid square images in tests
        pos = [7, 13, 9]
        cols = ['z', 'y', 'x']
        expected = DataFrame(np.asarray(pos).reshape(1, -1), columns=cols)

        image = np.ones(dims, dtype='uint8')
        draw_feature(image, pos, (21, 27, 27))
        actual = tp.locate(image, (7, 9, 9), 1, preprocess=False,
                           engine=self.engine)[cols]
        assert_allclose(actual, expected, atol=0.1)

    def test_reject_peaks_near_edge(self):
        """Check whether local maxima near the edge of the image
        are properly rejected, so as not to cause crashes later."""
        N = 30
        diameter = 9
        y = np.arange(20, 10*N + 1, 20)
        x = np.linspace(diameter / 2. - 2, diameter * 1.5, len(y))
        cols = ['y', 'x']
        expected = DataFrame(np.vstack([y, x]).T, columns=cols)

        dims = (y.max() - y.min() + 5*diameter, int(4 * diameter) - 2)
        image = np.ones(dims, dtype='uint8')
        for ypos, xpos in expected[['y', 'x']].values:
            draw_feature(image, [ypos, xpos], 27, max_value=100)
        def locate(image, **kwargs):
            return tp.locate(image, diameter, 1, preprocess=False,
                             engine=self.engine, **kwargs)[cols]
        # All we care about is that this doesn't crash
        actual = locate(image)
        assert len(actual)
        # Check the other sides
        actual = locate(np.fliplr(image))
        assert len(actual)
        actual = locate(image.transpose())
        assert len(actual)
        actual = locate(np.flipud(image.transpose()))
        assert len(actual)

    def test_subpx_precision(self):
        self.check_skip()
        L = 21
        dims = (L, L + 2)  # avoid square images in tests
        cols = ['y', 'x']
        PRECISION = 0.1

        # one bright pixel
        pos = [7, 13]
        image = np.ones(dims, dtype='uint8')
        draw_point(image, pos, 100)
        actual = tp.locate(image, 3, 1, preprocess=False,
                           engine=self.engine)[cols]
        expected = DataFrame(np.asarray(pos).reshape(1, -1), columns=cols)
        assert_allclose(actual, expected, atol=0.001)  # especially precise

        # two neighboring pixels of equal brightness
        pos1 = np.array([7, 13])
        pos2 = np.array([8, 13])
        pos = [7.5, 13]  # center is between pixels
        image = np.ones(dims, dtype='uint8')
        draw_point(image, pos1, 100)
        draw_point(image, pos2, 100)
        actual = tp.locate(image, 5, 1, preprocess=False,
                           engine=self.engine)[cols]
        expected = DataFrame(np.asarray(pos).reshape(1, -1), columns=cols)
        assert_allclose(actual, expected, atol=PRECISION)

        # two neighboring pixels of unequal brightness
        pos1 = np.array([7, 13])
        pos2 = np.array([8, 13])
        pos = [7.25, 13]  # center is between pixels, biased left
        image = np.ones(dims, dtype='uint8')
        draw_point(image, pos1, 100)
        draw_point(image, pos2, 50)
        actual = tp.locate(image, 5, 1, preprocess=False,
                           engine=self.engine)[cols]
        expected = DataFrame(np.asarray(pos).reshape(1, -1), columns=cols)
        assert_allclose(actual, expected, atol=PRECISION)

        pos = [7.75, 13]  # center is between pixels, biased right
        image = np.ones(dims, dtype='uint8')
        draw_point(image, pos1, 50)
        draw_point(image, pos2, 100)
        actual = tp.locate(image, 5, 1, preprocess=False,
                           engine=self.engine)[cols]
        expected = DataFrame(np.asarray(pos).reshape(1, -1), columns=cols)
        assert_allclose(actual, expected, atol=PRECISION)

        pos1 = np.array([7, 12])
        pos2 = np.array([7, 13])
        pos = [7, 12.25]  # center is between pixels, biased down
        image = np.ones(dims, dtype='uint8')
        draw_point(image, pos1, 100)
        draw_point(image, pos2, 50)
        actual = tp.locate(image, 5, 1, preprocess=False,
                           engine=self.engine)[cols]
        expected = DataFrame(np.asarray(pos).reshape(1, -1), columns=cols)
        assert_allclose(actual, expected, atol=PRECISION)

        pos = [7, 12.75]  # center is between pixels, biased up
        image = np.ones(dims, dtype='uint8')
        draw_point(image, pos1, 50)
        draw_point(image, pos2, 100)
        actual = tp.locate(image, 5, 1, preprocess=False,
                           engine=self.engine)[cols]
        expected = DataFrame(np.asarray(pos).reshape(1, -1), columns=cols)
        assert_allclose(actual, expected, atol=PRECISION)

        # four neighboring pixels of unequal brightness
        pos1 = np.array([7, 13])
        pos2 = np.array([8, 13])
        pos3 = np.array([7, 13])
        pos4 = np.array([8, 13])
        pos = [7.25, 13]  # center is between pixels, biased left
        image = np.ones(dims, dtype='uint8')
        draw_point(image, pos1, 100)
        draw_point(image, pos2, 50)
        draw_point(image, pos3, 100)
        draw_point(image, pos4, 50)
        actual = tp.locate(image, 5, 1, preprocess=False,
                           engine=self.engine)[cols]
        expected = DataFrame(np.asarray(pos).reshape(1, -1), columns=cols)
        assert_allclose(actual, expected, atol=PRECISION)

        pos = [7.75, 13]  # center is between pixels, biased right 
        image = np.ones(dims, dtype='uint8')
        draw_point(image, pos1, 50)
        draw_point(image, pos2, 100)
        draw_point(image, pos3, 50)
        draw_point(image, pos4, 100)
        actual = tp.locate(image, 5, 1, preprocess=False,
                           engine=self.engine)[cols]
        expected = DataFrame(np.asarray(pos).reshape(1, -1), columns=cols)
        assert_allclose(actual, expected, atol=PRECISION)

        pos1 = np.array([7, 12])
        pos2 = np.array([7, 13])
        pos3 = np.array([7, 12])
        pos4 = np.array([7, 13])
        pos = [7, 12.25]  # center is between pixels, biased down
        image = np.ones(dims, dtype='uint8')
        draw_point(image, pos1, 100)
        draw_point(image, pos2, 50)
        actual = tp.locate(image, 5, 1, preprocess=False,
                           engine=self.engine)[cols]
        expected = DataFrame(np.asarray(pos).reshape(1, -1), columns=cols)
        assert_allclose(actual, expected, atol=PRECISION)

        pos = [7, 12.75]  # center is between pixels, biased up 
        image = np.ones(dims, dtype='uint8')
        draw_point(image, pos1, 50)
        draw_point(image, pos2, 100)
        actual = tp.locate(image, 5, 1, preprocess=False,
                           engine=self.engine)[cols]
        expected = DataFrame(np.asarray(pos).reshape(1, -1), columns=cols)
        assert_allclose(actual, expected, atol=PRECISION)

    def test_multiple_simple_sparse(self):
        self.check_skip()
        actual, expected = compare((200, 300), 4, 2, noise_level=0,
                                   engine=self.engine)
        assert_allclose(actual, expected, atol=0.5)

    def test_multiple_noisy_sparse(self):
        self.check_skip()
        actual, expected = compare((200, 300), 4, 2, noise_level=1,
                                   engine=self.engine)
        assert_allclose(actual, expected, atol=0.5)

    def test_multiple_more_noisy_sparse(self):
        self.check_skip()
        actual, expected = compare((200, 300), 4, 2, noise_level=2,
                                   engine=self.engine)
        assert_allclose(actual, expected, atol=0.5)

    def test_multiple_anisotropic_3D_simple(self):
        self.skip_numba()
        actual, expected = compare((100, 120, 10), 4, (4, 4, 2), noise_level=0,
                                   engine=self.engine)
        assert_allclose(actual, expected, atol=0.5)

    def test_topn(self):
        self.check_skip()
        L = 21
        dims = (L, L + 2)  # avoid square images in tests
        cols = ['y', 'x']
        PRECISION = 0.1

        # top 2
        pos1 = np.array([7, 7])
        pos2 = np.array([14, 14])
        pos3 = np.array([7, 14])
        image = np.ones(dims, dtype='uint8')
        draw_point(image, pos1, 100)
        draw_point(image, pos2, 90)
        draw_point(image, pos3, 80)
        actual = tp.locate(image, 5, 1, topn=2, preprocess=False,
                           engine=self.engine)[cols]
        actual = actual.sort(['x', 'y'])  # sort for reliable comparison
        expected = DataFrame([pos1, pos2], columns=cols).sort(['x', 'y'])
        assert_allclose(actual, expected, atol=PRECISION)

        # top 1
        actual = tp.locate(image, 5, 1, topn=1, preprocess=False,
                           engine=self.engine)[cols]
        actual = actual.sort(['x', 'y'])  # sort for reliable comparison
        expected = DataFrame([pos1], columns=cols).sort(['x', 'y'])
        assert_allclose(actual, expected, atol=PRECISION)

    def test_rg(self):
        # To draw Gaussians with radii 2, 3, 5, and 7 px, we supply the draw
        # function with rg=0.25. This means that the radius of gyration will be
        # one fourth of the max radius in the draw area, which is diameter/2.

        # The 'size' comes out to be within 3%, which is because of the
        # pixelation of the Gaussian.

        # The IDL code has mistake in this area, documented here:
        # http://www.physics.emory.edu/~weeks/idl/radius.html

        self.check_skip()
        L = 101
        dims = (L, L + 2)  # avoid square images in tests
        for pos in [[50, 55], [50.2, 55], [50.5, 55]]:
            for SIZE in [2, 3, 5, 7]:
                image = np.zeros(dims, dtype='uint8')
                draw_feature(image, pos, SIZE*8, rg=0.25)
                actual = tp.locate(image, SIZE*8 - 1, 1, preprocess=False,
                                   engine=self.engine)['size']
                assert_allclose(actual, SIZE, rtol=0.1)

    def test_eccentricity(self):
        # Eccentricity (elongation) is measured with good accuracy and
        # ~0.02 precision, as long as the mask is large enough to cover
        # the whole object.
        self.check_skip()
        L = 501 
        dims = (L + 2, L)  # avoid square images in tests
        pos = [50, 55]
        cols = ['y', 'x']

        ECC = 0
        image = np.ones(dims, dtype='uint8')
        draw_feature(image, pos, 27, ecc=ECC)
        actual = tp.locate(image, 21, 1, preprocess=False,
                           engine=self.engine)['ecc']
        expected = ECC
        assert_allclose(actual, expected, atol=0.02)

        ECC = 0.2
        image = np.ones(dims, dtype='uint8')
        draw_feature(image, pos, 27, ecc=ECC)
        actual = tp.locate(image, 21, 1, preprocess=False,
                           engine=self.engine)['ecc']
        expected = ECC
        assert_allclose(actual, expected, atol=0.1)

        ECC = 0.5
        image = np.ones(dims, dtype='uint8')
        draw_feature(image, pos, 27, ecc=ECC)
        actual = tp.locate(image, 21, 1, preprocess=False,
                           engine=self.engine)['ecc']
        expected = ECC
        assert_allclose(actual, expected, atol=0.1)


    def test_whole_pixel_shifts(self):
        self.check_skip()
        L = 21
        dims = (L, L + 2)  # avoid square images in tests
        pos = [7, 13]
        expected = np.array([pos])

        image = np.ones(dims, dtype='uint8')
        draw_feature(image, pos, 15)

        guess = np.array([[6, 13]])
        actual = tp.feature.refine(image, image, 6, guess, characterize=False,
                                   engine=self.engine)[:, :2][:, ::-1]
        assert_allclose(actual, expected, atol=0.1)

        guess = np.array([[7, 12]])
        actual = tp.feature.refine(image, image, 6, guess, characterize=False,
                                   engine=self.engine)[:, :2][:, ::-1]
        assert_allclose(actual, expected, atol=0.1)

        guess = np.array([[7, 14]])
        actual = tp.feature.refine(image, image, 6, guess, characterize=False,
                                   engine=self.engine)[:, :2][:, ::-1]
        assert_allclose(actual, expected, atol=0.1)

        guess = np.array([[6, 12]])
        actual = tp.feature.refine(image, image, 6, guess, characterize=False,
                                   engine=self.engine)[:, :2][:, ::-1]
        assert_allclose(actual, expected, atol=0.1)


class TestFeatureIdentificationWithVanillaNumpy(
    CommonFeatureIdentificationTests, unittest.TestCase):

    def setUp(self):
        self.engine = 'python'


class TestFeatureIdentificationWithNumba(
    CommonFeatureIdentificationTests, unittest.TestCase):

    def setUp(self):
        self.engine = 'numba'

    def check_skip(self):
        if not NUMBA_AVAILABLE:
            raise nose.SkipTest("Numba not installed. Skipping.")

    def skip_numba(self):
        raise nose.SkipTest("This feature is not "
                            "supported by the numba variant. Skipping.")


if __name__ == '__main__':
    import nose
    nose.runmodule(argv=[__file__, '-vvs', '-x', '--pdb', '--pdb-failure'],
                   exit=False)
