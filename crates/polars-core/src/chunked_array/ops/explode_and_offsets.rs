use arrow::bitmap::MutableBitmap;
use arrow::compute::cast::utf8view_to_utf8;
#[cfg(feature = "dtype-array")]
use arrow::legacy::compute::take::take_unchecked;
use polars_utils::vec::PushUnchecked;

use super::*;

impl ChunkExplode for ListChunked {
    fn offsets(&self) -> PolarsResult<OffsetsBuffer<i64>> {
        let ca = self.rechunk();
        let listarr: &LargeListArray = ca.downcast_iter().next().unwrap();
        let offsets = listarr.offsets().clone();

        Ok(offsets)
    }

    fn explode_and_offsets(&self) -> PolarsResult<(Series, OffsetsBuffer<i64>)> {
        // A list array's memory layout is actually already 'exploded', so we can just take the values array
        // of the list. And we also return a slice of the offsets. This slice can be used to find the old
        // list layout or indexes to expand the DataFrame in the same manner as the 'explode' operation
        let ca = self.rechunk();
        let listarr: &LargeListArray = ca.downcast_iter().next().unwrap();
        let offsets_buf = listarr.offsets().clone();
        let offsets = listarr.offsets().as_slice();
        let mut values = listarr.values().clone();

        let mut s = if ca._can_fast_explode() {
            // ensure that the value array is sliced
            // as a list only slices its offsets on a slice operation

            // we only do this in fast-explode as for the other
            // branch the offsets must coincide with the values.
            if !offsets.is_empty() {
                let start = offsets[0] as usize;
                let len = offsets[offsets.len() - 1] as usize - start;
                // safety:
                // we are in bounds
                values = unsafe { values.sliced_unchecked(start, len) };
            }
            // safety: inner_dtype should be correct
            unsafe {
                Series::from_chunks_and_dtype_unchecked(
                    self.name(),
                    vec![values],
                    &self.inner_dtype().to_physical(),
                )
            }
        } else {
            // during tests
            // test that this code branch is not hit with list arrays that could be fast exploded
            #[cfg(test)]
            {
                let mut last = offsets[0];
                let mut has_empty = false;
                for &o in &offsets[1..] {
                    if o == last {
                        has_empty = true;
                    }
                    last = o;
                }
                if !has_empty && offsets[0] == 0 {
                    panic!("could have fast exploded")
                }
            }

            // safety: inner_dtype should be correct
            let values = unsafe {
                Series::from_chunks_and_dtype_unchecked(
                    self.name(),
                    vec![values],
                    &self.inner_dtype().to_physical(),
                )
            };
            values.explode_by_offsets(offsets)
        };
        debug_assert_eq!(s.name(), self.name());
        // restore logical type
        unsafe {
            s = s.cast_unchecked(&self.inner_dtype()).unwrap();
        }

        Ok((s, offsets_buf))
    }
}

#[cfg(feature = "dtype-array")]
impl ChunkExplode for ArrayChunked {
    fn offsets(&self) -> PolarsResult<OffsetsBuffer<i64>> {
        // fast-path for non-null array.
        if self.null_count() == 0 {
            let width = self.width() as i64;
            let offsets = (0..self.len() + 1)
                .map(|i| {
                    let i = i as i64;
                    i * width
                })
                .collect::<Vec<_>>();
            // safety: monotonically increasing
            let offsets = unsafe { OffsetsBuffer::new_unchecked(offsets.into()) };

            return Ok(offsets);
        }

        let ca = self.rechunk();
        let arr = ca.downcast_iter().next().unwrap();
        // we have already ensure that validity is not none.
        let validity = arr.validity().unwrap();
        let width = arr.size();

        let mut current_offset = 0i64;
        let offsets = (0..=arr.len())
            .map(|i| {
                if i == 0 {
                    return current_offset;
                }
                // Safety: we are within bounds
                if unsafe { validity.get_bit_unchecked(i - 1) } {
                    current_offset += width as i64
                }
                current_offset
            })
            .collect::<Vec<_>>();
        // safety: monotonically increasing
        let offsets = unsafe { OffsetsBuffer::new_unchecked(offsets.into()) };
        Ok(offsets)
    }

    fn explode_and_offsets(&self) -> PolarsResult<(Series, OffsetsBuffer<i64>)> {
        let ca = self.rechunk();
        let arr = ca.downcast_iter().next().unwrap();
        // fast-path for non-null array.
        if arr.null_count() == 0 {
            let s = Series::try_from((self.name(), arr.values().clone()))
                .unwrap()
                .cast(&ca.inner_dtype())?;
            let width = self.width() as i64;
            let offsets = (0..self.len() + 1)
                .map(|i| {
                    let i = i as i64;
                    i * width
                })
                .collect::<Vec<_>>();
            // safety: monotonically increasing
            let offsets = unsafe { OffsetsBuffer::new_unchecked(offsets.into()) };
            return Ok((s, offsets));
        }

        // we have already ensure that validity is not none.
        let validity = arr.validity().unwrap();
        let values = arr.values();
        let width = arr.size();

        let mut indices = MutablePrimitiveArray::<IdxSize>::with_capacity(
            values.len() - arr.null_count() * (width - 1),
        );
        let mut offsets = Vec::with_capacity(arr.len() + 1);
        let mut current_offset = 0i64;
        offsets.push(current_offset);
        (0..arr.len()).for_each(|i| {
            // Safety: we are within bounds
            if unsafe { validity.get_bit_unchecked(i) } {
                let start = (i * width) as IdxSize;
                let end = start + width as IdxSize;
                indices.extend_trusted_len_values(start..end);
                current_offset += width as i64;
            } else {
                indices.push_null();
            }
            offsets.push(current_offset);
        });

        // Safety: the indices we generate are in bounds
        let chunk = unsafe { take_unchecked(&**values, &indices.into()) };
        // safety: monotonically increasing
        let offsets = unsafe { OffsetsBuffer::new_unchecked(offsets.into()) };

        Ok((
            // Safety: inner_dtype should be correct
            unsafe {
                Series::from_chunks_and_dtype_unchecked(ca.name(), vec![chunk], &ca.inner_dtype())
            },
            offsets,
        ))
    }
}

impl ChunkExplode for StringChunked {
    fn offsets(&self) -> PolarsResult<OffsetsBuffer<i64>> {
        let mut offsets = Vec::with_capacity(self.len() + 1);
        let mut length_so_far = 0;
        offsets.push(length_so_far);

        for arr in self.downcast_iter() {
            for len in arr.len_iter() {
                // SAFETY:
                // pre-allocated
                unsafe { offsets.push_unchecked(length_so_far) };
                length_so_far += len as i64;
            }
        }

        // SAFETY:
        // Monotonically increasing.
        unsafe { Ok(OffsetsBuffer::new_unchecked(offsets.into())) }
    }

    fn explode_and_offsets(&self) -> PolarsResult<(Series, OffsetsBuffer<i64>)> {
        // A list array's memory layout is actually already 'exploded', so we can just take the values array
        // of the list. And we also return a slice of the offsets. This slice can be used to find the old
        // list layout or indexes to expand the DataFrame in the same manner as the 'explode' operation
        let ca = self.rechunk();
        let array = ca.downcast_iter().next().unwrap();
        // TODO! maybe optimize for new utf8view?
        let array = utf8view_to_utf8(array);

        let values = array.values();
        let old_offsets = array.offsets().clone();

        let (new_offsets, validity) = if let Some(validity) = array.validity() {
            // capacity estimate
            let capacity = self.get_values_size() + validity.unset_bits();

            let old_offsets = old_offsets.as_slice();
            let mut old_offset = old_offsets[0];
            let mut new_offsets = Vec::with_capacity(capacity + 1);
            new_offsets.push(old_offset);

            let mut bitmap = MutableBitmap::with_capacity(capacity);
            let values = values.as_slice();
            for (&offset, valid) in old_offsets[1..].iter().zip(validity) {
                // safety:
                // new_offsets already has a single value, so -1 is always in bounds
                let latest_offset = unsafe { *new_offsets.get_unchecked(new_offsets.len() - 1) };

                if valid {
                    debug_assert!(old_offset as usize <= values.len());
                    debug_assert!(offset as usize <= values.len());
                    let val = unsafe { values.get_unchecked(old_offset as usize..offset as usize) };

                    // take the string value and find the char offsets
                    // create a new offset value for each char boundary
                    // safety:
                    // we know we have string data.
                    let str_val = unsafe { std::str::from_utf8_unchecked(val) };

                    let char_offsets = str_val
                        .char_indices()
                        .skip(1)
                        .map(|t| t.0 as i64 + latest_offset);

                    // extend the chars
                    // also keep track of the amount of offsets added
                    // as we must update the validity bitmap
                    let len_before = new_offsets.len();
                    new_offsets.extend(char_offsets);
                    new_offsets.push(latest_offset + str_val.len() as i64);
                    bitmap.extend_constant(new_offsets.len() - len_before, true);
                } else {
                    // no data, just add old offset and set null bit
                    new_offsets.push(latest_offset);
                    bitmap.push(false)
                }
                old_offset = offset;
            }

            (new_offsets.into(), bitmap.into())
        } else {
            // fast(er) explode

            // we cannot naively explode, because there might be empty strings.

            // capacity estimate
            let capacity = self.get_values_size();
            let old_offsets = old_offsets.as_slice();
            let mut old_offset = old_offsets[0];
            let mut new_offsets = Vec::with_capacity(capacity + 1);
            new_offsets.push(old_offset);

            let values = values.as_slice();
            for &offset in &old_offsets[1..] {
                // safety:
                // new_offsets already has a single value, so -1 is always in bounds
                let latest_offset = unsafe { *new_offsets.get_unchecked(new_offsets.len() - 1) };
                debug_assert!(old_offset as usize <= values.len());
                debug_assert!(offset as usize <= values.len());
                let val = unsafe { values.get_unchecked(old_offset as usize..offset as usize) };

                // take the string value and find the char offsets
                // create a new offset value for each char boundary
                // safety:
                // we know we have string data.
                let str_val = unsafe { std::str::from_utf8_unchecked(val) };

                let char_offsets = str_val
                    .char_indices()
                    .skip(1)
                    .map(|t| t.0 as i64 + latest_offset);

                // extend the chars
                new_offsets.extend(char_offsets);
                new_offsets.push(latest_offset + str_val.len() as i64);
                old_offset = offset;
            }

            (new_offsets.into(), None)
        };

        let array = unsafe {
            Utf8Array::<i64>::from_data_unchecked_default(new_offsets, values.clone(), validity)
        };

        let new_arr = Box::new(array) as ArrayRef;

        let s = Series::try_from((self.name(), new_arr)).unwrap();
        Ok((s, old_offsets))
    }
}
