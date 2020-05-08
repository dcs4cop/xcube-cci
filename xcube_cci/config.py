# The MIT License (MIT)
# Copyright (c) 2020 by the xcube development team and contributors
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of
# this software and associated documentation files (the "Software"), to deal in
# the Software without restriction, including without limitation the rights to
# use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies
# of the Software, and to permit persons to whom the Software is furnished to do
# so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import math
from typing import Tuple, Union, Optional, Sequence, Dict, Any

import pandas as pd

from .constants import CCI_MAX_IMAGE_SIZE, DEFAULT_TILE_SIZE


def _safe_int_div(x: int, y: int) -> int:
    return (x + y - 1) // y


class CubeConfig:
    """
    CCI Open Data Portal cube configuration.

    :param dataset_name: Dataset name. Mandatory.
    :param variable_names: Variable names. Mandatory.
    :param variable_units: Variable units. Optional.
    :param variable_data_types: Variable data types. Optional.
    :param tile_size: Tile size as tuple (width, height). Optional.
    :param geometry:
    :param spatial_res:
    :param crs:
    :param time_range:
    :param time_period:
    :param collection_id:
    :param four_d:
    :param exception_type:
    """

    def __init__(self,
                 dataset_name: str = None,
                 variable_names: Sequence[str] = None,
                 variable_units: Union[str, Sequence[str]] = None,
                 variable_data_types: Union[str, Sequence[str]] = None,
                 tile_size: Union[str, Tuple[int, int]] = None,
                 geometry: Union[str, Tuple[float, float, float, float]] = None,
                 time_range: Union[str, pd.Timestamp, Tuple[str, str], Tuple[pd.Timestamp, pd.Timestamp]] = None,
                 collection_id: str = None,
                 four_d: bool = False,
                 exception_type=ValueError):

        if not dataset_name:
            raise exception_type('dataset name must be given')
        if not variable_names:
            raise exception_type('variable names must be a given')
        if not geometry:
            raise exception_type('geometry must be given')
        if not time_range:
            raise exception_type('time range must be given')

        if isinstance(geometry, str):
            x1, y1, x2, y2 = tuple(map(float, geometry.split(',', maxsplit=3)))
        elif geometry:
            x1, y1, x2, y2 = geometry
        else:
            x1 = -180
            y1 = -90
            x2 = 180
            y2 = 90
            geometry = x1, y1, x2, y2


        if tile_size is None:
            tile_width, tile_height = None, None
        elif isinstance(tile_size, str):
            parsed = tuple(map(int, geometry.split(',', maxsplit=1)))
            if len(parsed) == 1:
                tile_width, tile_height = parsed[0], parsed[0]
            elif len(parsed) == 2:
                tile_width, tile_height = parsed
            else:
                raise exception_type(f'invalid tile size: {tile_size}')
        else:
            tile_width, tile_height = tile_size
        if tile_width is None:
            tile_width = DEFAULT_TILE_SIZE
        if tile_height is None:
            tile_height = DEFAULT_TILE_SIZE
        if tile_width > CCI_MAX_IMAGE_SIZE:
            tile_width = CCI_MAX_IMAGE_SIZE
        if tile_height > CCI_MAX_IMAGE_SIZE:
            tile_height = CCI_MAX_IMAGE_SIZE

        geometry = x1, y1, x2, y2

        if isinstance(time_range, str):
            time_range = tuple(map(lambda s: s.strip(),
                                   time_range.split(',', maxsplit=1) if ',' in time_range else (
                                       time_range, time_range)))
            time_range = tuple(time_range)
        if len(time_range) == 1:
            time_range = time_range + time_range
        if len(time_range) != 2:
            exception_type('Time range must be have two elements')

        start_time, end_time = tuple(time_range)
        if isinstance(start_time, str) or isinstance(end_time, str):
            def convert_time(time_str):
                return pd.to_datetime(time_str, utc=True)

            start_time, end_time = tuple(map(convert_time, time_range))

        time_range = start_time, end_time

        self._dataset_name = dataset_name
        self._variable_names = tuple(variable_names)
        self._variable_units = variable_units or None
        self._variable_data_types = variable_data_types or None
        self._geometry = geometry
        self._time_range = time_range
        self._collection_id = collection_id
        self._four_d = four_d
        self._tile_size = tile_width, tile_height

    @classmethod
    def from_dict(cls, cube_config_dict: Dict[str, Any], exception_type=ValueError) -> 'CubeConfig':
        code = CubeConfig.__init__.__code__
        valid_keywords = set(code.co_varnames[1: code.co_argcount])
        given_keywords = set(cube_config_dict.keys())
        for keyword in cube_config_dict.keys():
            if keyword in valid_keywords:
                given_keywords.remove(keyword)
        if len(given_keywords) == 1:
            raise exception_type(f'Found invalid parameter {given_keywords.pop()!r} in cube configuration')
        elif len(given_keywords) > 1:
            given_keywords_text = ', '.join(map(lambda s: f'{s!r}', sorted(given_keywords)))
            raise exception_type(f'Found invalid parameters in cube configuration: {given_keywords_text}')
        return CubeConfig(exception_type=exception_type, **cube_config_dict)

    def as_dict(self) -> Dict[str, Any]:
        """Convert to JSON-serializable dictionary that can be passed to ctor as kwargs"""
        time_range = (self.time_range[0].isoformat(), self.time_range[1].isoformat()) \
            if self.time_range else None
        return dict(dataset_name=self.dataset_name,
                    variable_names=self.variable_names,
                    variable_units=self.variable_units,
                    variable_data_types=self.variable_data_types,
                    tile_size=self.tile_size,
                    geometry=self.geometry,
                    time_range=time_range,
                    collection_id=self.collection_id,
                    four_d=self.four_d)

    @property
    def dataset_name(self) -> str:
        return self._dataset_name

    @property
    def variable_names(self) -> Tuple[str, ...]:
        return self._variable_names

    @property
    def variable_units(self) -> Optional[Union[str, Tuple[str, ...]]]:
        return self._variable_units

    @property
    def variable_data_types(self) -> Optional[Union[str, Tuple[str, ...]]]:
        return self._variable_data_types

    @property
    def geometry(self) -> Tuple[float, float, float, float]:
        return self._geometry

    @property
    def time_range(self) -> Tuple[pd.Timestamp, pd.Timestamp]:
        return self._time_range

    @property
    def collection_id(self) -> Optional[str]:
        return self._collection_id

    @property
    def four_d(self) -> bool:
        return self._four_d

    @property
    def tile_size(self) -> Tuple[int, int]:
        return self._tile_size

    @property
    def is_wgs84_crs(self) -> bool:
        return True

    @classmethod
    def _adjust_size(cls, size: int, tile_size: int) -> int:
        if size > tile_size:
            num_tiles = _safe_int_div(size, tile_size)
            size = num_tiles * tile_size
        return size
