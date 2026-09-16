import unittest
import numpy as np
from lab.mining import _ctz,njit
from lab.standard_masks import mask_economics

@njit(cache=True)
def indexes(words):
    return np.array([_ctz(x) for x in words],dtype=np.int64)

class BitScan(unittest.TestCase):
    def test_all_bit_positions_and_random_unsigned_words(self):
        rng=np.random.default_rng(741)
        words=np.r_[np.array([1<<i for i in range(64)],np.uint64),rng.integers(1,2**64,size=4096,dtype=np.uint64)]
        expected=np.array([(int(x)&-int(x)).bit_length()-1 for x in words])
        np.testing.assert_array_equal(indexes(words),expected)

    def test_first_trade_and_positive_upper_across_empty_and_multiword_matches(self):
        # Empty match, late bit in the next match, then a later positive offer
        # in the same match: first-trade loss must not replace the true bound.
        words=np.array([0,1<<63,1,0,1<<31],np.uint64)
        ends=np.array([1,3,3,4,5],np.int64)
        payoff=np.zeros(320,np.int64);payoff[127]=-200;payoff[128]=246;payoff[287]=115
        self.assertEqual(mask_economics(words,ends,payoff),(2,-85,361))
        rng=np.random.default_rng(822)
        ends=np.array([2,2,5,5,5,6],np.int64)
        for _ in range(32):
            words=rng.integers(0,2**64,size=6,dtype=np.uint64)
            payoff=rng.integers(-400,700,size=384,dtype=np.int64)
            bits=np.unpackbits(words.view(np.uint8),bitorder='little').astype(bool)
            n=net=upper=0;start=0
            while start<len(words):
                end=int(ends[start]);hits=np.flatnonzero(bits[start*64:end*64])+start*64
                if len(hits):n+=1;net+=int(payoff[hits[0]]);upper+=max(0,int(payoff[hits].max()))
                start=end
            self.assertEqual(mask_economics(words,ends,payoff),(n,net,upper))

if __name__=='__main__':unittest.main()
