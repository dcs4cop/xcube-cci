# The MIT License (MIT)
# Copyright (c) 2019 by the xcube development team and contributors
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

import bisect
from datetime import datetime
from dateutil.relativedelta import relativedelta
import itertools
import json
import time
from abc import abstractmethod, ABCMeta
from collections import MutableMapping
from typing import Iterator, Any, List, Dict, Tuple, Callable, Iterable, KeysView, Union

import numpy as np
import pandas as pd
import re

from .config import CubeConfig
from .constants import DATA_ARRAY_NAME
from .cciodp import CciOdp

_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S"


def _dict_to_bytes(d: Dict):
    return _str_to_bytes(json.dumps(d, indent=2))


def _str_to_bytes(s: str):
    return bytes(s, encoding='utf-8')


# todo move this to xcube
class RemoteStore(MutableMapping, metaclass=ABCMeta):
    """
    A remote Zarr Store.

    :param cube_config: Cube configuration.
    :param observer: An optional callback function called when remote requests are mode: observer(**kwargs).
    :param trace_store_calls: Whether store calls shall be printed (for debugging).
    """

    def __init__(self,
                 cube_config: CubeConfig,
                 observer: Callable = None,
                 trace_store_calls=False):

        self._cube_config = cube_config
        self._observers = [observer] if observer is not None else []
        self._trace_store_calls = trace_store_calls
        self._time_ranges = self.get_time_ranges()

        if not self._time_ranges:
            raise ValueError('Could not determine any valid time stamps')

        t_array = np.array([s + 0.5 * (e - s) for s, e in self._time_ranges]).astype('datetime64[s]').astype(np.int64)
        t_bnds_array = np.array(self._time_ranges).astype('datetime64[s]').astype(np.int64)
        x1, y1, x2, y2 = self._cube_config.geometry
        time_coverage_start = self._time_ranges[0][0]
        time_coverage_end = self._time_ranges[-1][1]
        # todo add processing_level from metadata
        global_attrs = dict(
            Conventions='CF-1.7',
            coordinates='time_bnds',
            title=f'{self._cube_config.dataset_name} Data Cube Subset',
            history=[
                dict(
                    program=f'{self._class_name}',
                    cube_config=self._cube_config.as_dict(),
                )
            ],
            date_created=pd.Timestamp.now().isoformat(),
            time_coverage_start=time_coverage_start.isoformat(),
            time_coverage_end=time_coverage_end.isoformat(),
            time_coverage_duration=(time_coverage_end - time_coverage_start).isoformat(),
        )

        if self._cube_config.is_wgs84_crs:
            x1, y2, x2, y2 = self._cube_config.geometry
            global_attrs.update(geospatial_lon_min=x1,
                                geospatial_lat_min=y1,
                                geospatial_lon_max=x2,
                                geospatial_lat_max=y2)

        # setup Virtual File System (vfs)
        self._vfs = {
            '.zgroup': _dict_to_bytes(dict(zarr_format=2)),
            '.zattrs': _dict_to_bytes(global_attrs)
        }

        width = -1
        height = -1
        self._dimension_data = self.get_dimension_data()
        self._dim_flipped = {}
        for dimension_name in self._dimension_data:
            if dimension_name == 'lat' or dimension_name == 'latitude':
                dim_attrs = self.get_attrs(dimension_name)
                dim_attrs['_ARRAY_DIMENSIONS'] = dimension_name
                dimension_data = self._dimension_data[dimension_name]
                self._dim_flipped[dimension_name] = dimension_data[0] > dimension_data[-1]
                if self._dim_flipped[dimension_name]:
                    dimension_data.reverse()
                lat_start_offset = bisect.bisect_right(dimension_data, y1)
                lat_end_offset = bisect.bisect_right(dimension_data, y2)
                lat_array = np.array(dimension_data[lat_start_offset:lat_end_offset])
                height = len(lat_array)
                self._add_static_array('lat', lat_array, dim_attrs)
            if dimension_name == 'lon' or dimension_name == 'longitude':
                dim_attrs = self.get_attrs(dimension_name)
                dim_attrs['_ARRAY_DIMENSIONS'] = dimension_name
                dimension_data = self._dimension_data[dimension_name]
                self._dim_flipped[dimension_name] = dimension_data[0] > dimension_data[-1]
                if self._dim_flipped[dimension_name]:
                    dimension_data.reverse()
                lon_start_offset = bisect.bisect_right(dimension_data, x1)
                lon_end_offset = bisect.bisect_right(dimension_data, x2)
                lon_array = np.array(dimension_data[lon_start_offset:lon_end_offset])
                width = len(lon_array)
                self._add_static_array('lon', lon_array, dim_attrs)
        if width == -1:
            raise ValueError('Could not determine latitude. Does this dataset have this dimension?')
        if width == -1:
            raise ValueError('Could not determine longitude. Does this dataset have this dimension?')
        time_attrs = {
            "_ARRAY_DIMENSIONS": ['time'],
            "units": "seconds since 1970-01-01T00:00:00Z",
            "calendar": "proleptic_gregorian",
            "standard_name": "time",
            "bounds": "time_bnds",
        }
        time_bnds_attrs = {
            "_ARRAY_DIMENSIONS": ['time', 'bnds'],
            "units": "seconds since 1970-01-01T00:00:00Z",
            "calendar": "proleptic_gregorian",
            "standard_name": "time",
        }

        self._add_static_array('time', t_array, time_attrs)
        self._add_static_array('time_bnds', t_bnds_array, time_bnds_attrs)

        self._tile_width, self._tile_height = self._cube_config.tile_size
        if width < 1.5 * self._tile_width:
            self._tile_width = width
        else:
            width = self._adjust_size(width, self._tile_width)
        if height < 1.5 * self._tile_height:
            self._tile_height = height
        else:
            height = self._adjust_size(height, self._tile_height)

        if self._cube_config.four_d:
            if self._cube_config.is_wgs84_crs:
                var_array_dimensions = ['time', 'lat', 'lon', 'var']
            else:
                var_array_dimensions = ['time', 'y', 'x', 'var']
            num_vars = len(self._cube_config.variable_names)
            self._add_static_array('variable',
                                   np.array(self._cube_config.variable_names),
                                   attrs=dict(_ARRAY_DIMENSIONS=['variable']))
            var_encoding = self.get_encoding(DATA_ARRAY_NAME)
            var_attrs = self.get_attrs(DATA_ARRAY_NAME)
            var_attrs.update(_ARRAY_DIMENSIONS=var_array_dimensions, var_names=self._cube_config.variable_names)
            self._add_remote_array(DATA_ARRAY_NAME,
                                   [t_array.size, height, width, num_vars],
                                   [1, self._tile_height, self._tile_width, num_vars],
                                   var_encoding,
                                   var_attrs)
        else:
            if self._cube_config.is_wgs84_crs:
                var_array_dimensions = ['time', 'lat', 'lon']
            else:
                var_array_dimensions = ['time', 'y', 'x']
            for variable_name in self._cube_config.variable_names:
                var_encoding = self.get_encoding(variable_name)
                var_attrs = self.get_attrs(variable_name)
                var_attrs.update(_ARRAY_DIMENSIONS=var_array_dimensions)
                self._add_remote_array(variable_name,
                                       [t_array.size, height, width],
                                       [1, self._tile_height, self._tile_width],
                                       var_encoding,
                                       var_attrs)

    @classmethod
    def _adjust_size(cls, size: int, tile_size: int) -> int:
        if size > tile_size:
            num_tiles = cls._safe_int_div(size, tile_size)
            size = num_tiles * tile_size
        return size

    @classmethod
    def _safe_int_div(cls, x: int, y: int) -> int:
        return (x + y - 1) // y

    def get_time_ranges(self) -> List[Tuple]:
        time_start, time_end = self._cube_config.time_range
        time_period = self._cube_config.time_period
        request_time_ranges = []
        time_now = time_start
        while time_now <= time_end:
            time_next = time_now + time_period
            request_time_ranges.append((time_now, time_next))
            time_now = time_next
        return request_time_ranges

    def get_spatial_lat_res(self):
        return self._cube_config.spatial_res

    def get_spatial_lon_res(self):
        return self._cube_config.spatial_res

    def get_dimension_data(self):
        return {}

    def add_observer(self, observer: Callable):
        """
        Add a request observer.

        :param observer: A callback function called when remote requests are mode: observer(**kwargs).
        """
        self._observers.append(observer)

    @abstractmethod
    def get_encoding(self, band_name: str) -> Dict[str, Any]:
        """
        Get the encoding settings for band (variable) *band_name*.
        Must at least contain "dtype" whose value is a numpy array-protocol type string.
        Refer to https://docs.scipy.org/doc/numpy/reference/arrays.interface.html#arrays-interface
        and zarr format 2 spec.
        """

    @abstractmethod
    def get_attrs(self, band_name: str) -> Dict[str, Any]:
        """
        Get any metadata attributes for band (variable) *band_name*.
        """

    def request_bbox(self, x_tile_index: int, y_tile_index: int) -> Tuple[float, float, float, float]:
        x_index = x_tile_index * self._tile_width
        y_index = y_tile_index * self._tile_height

        x01, _, _, y02 = self.cube_config.geometry
        spatial_lat_res = self.get_spatial_lat_res()
        spatial_lon_res = self.get_spatial_lon_res()

        x1 = x01 + spatial_lon_res * x_index
        x2 = x01 + spatial_lon_res * (x_index + self._tile_width)
        y1 = y02 - spatial_lat_res * (y_index + self._tile_height)
        y2 = y02 - spatial_lat_res * y_index

        return x1, y1, x2, y2

    def request_time_range(self, time_index: int) -> Tuple[pd.Timestamp, pd.Timestamp]:
        start_time, end_time = self._time_ranges[time_index]
        # if self.cube_config.time_tolerance:
        #     start_time -= self.cube_config.time_tolerance
        #     end_time += self.cube_config.time_tolerance
        return start_time, end_time

    def _add_static_array(self, name: str, array: np.ndarray, attrs: Dict):
        shape = list(map(int, array.shape))
        dtype = str(array.dtype.str)
        array_metadata = {
            "zarr_format": 2,
            "chunks": shape,
            "shape": shape,
            "dtype": dtype,
            "fill_value": None,
            "compressor": None,
            "filters": None,
            "order": "C",
        }
        self._vfs[name] = _str_to_bytes('')
        self._vfs[name + '/.zarray'] = _dict_to_bytes(array_metadata)
        self._vfs[name + '/.zattrs'] = _dict_to_bytes(attrs)
        self._vfs[name + '/' + ('.'.join(['0'] * array.ndim))] = bytes(array)

    def _add_remote_array(self,
                          name: str,
                          shape: List[int],
                          chunks: List[int],
                          encoding: Dict[str, Any],
                          attrs: Dict):
        array_metadata = dict(zarr_format=2,
                              shape=shape,
                              chunks=chunks,
                              compressor=None,
                              fill_value=None,
                              filters=None,
                              order='C')
        array_metadata.update(encoding)
        self._vfs[name] = _str_to_bytes('')
        self._vfs[name + '/.zarray'] = _dict_to_bytes(array_metadata)
        self._vfs[name + '/.zattrs'] = _dict_to_bytes(attrs)
        nums = np.array(shape) // np.array(chunks)
        indexes = itertools.product(*tuple(map(range, map(int, nums))))
        for index in indexes:
            filename = '.'.join(map(str, index))
            # noinspection PyTypeChecker
            self._vfs[name + '/' + filename] = name, index

    @property
    def cube_config(self) -> CubeConfig:
        return self._cube_config

    def _fetch_chunk(self, band_name: str, chunk_index: Tuple[int, ...]) -> bytes:
        if len(chunk_index) == 4:
            time_index, y_chunk_index, x_chunk_index, band_index = chunk_index
        else:
            time_index, y_chunk_index, x_chunk_index = chunk_index

        request_bbox = self.request_bbox(x_chunk_index, y_chunk_index)
        request_time_range = self.request_time_range(time_index)

        t0 = time.perf_counter()
        try:
            exception = None
            chunk_data = self.fetch_chunk(band_name,
                                          chunk_index,
                                          bbox=request_bbox,
                                          time_range=request_time_range)
        except Exception as e:
            exception = e
            chunk_data = None
        duration = time.perf_counter() - t0

        for observer in self._observers:
            observer(band_name=band_name,
                     chunk_index=chunk_index,
                     bbox=request_bbox,
                     time_range=request_time_range,
                     duration=duration,
                     exception=exception)

        if exception:
            raise exception

        return chunk_data

    @abstractmethod
    def fetch_chunk(self,
                    band_name: str,
                    chunk_index: Tuple[int, ...],
                    bbox: Tuple[float, float, float, float],
                    time_range: Tuple[pd.Timestamp, pd.Timestamp]) -> bytes:
        """
        Fetch chunk data from remote.

        :param band_name: Band name
        :param chunk_index: 3D chunk index (time, y, x)
        :param bbox: Requested bounding box in coordinate units of the CRS
        :param time_range: Requested time range
        :return: chunk data as raw bytes
        """
        pass

    @property
    def _class_name(self):
        return self.__module__ + '.' + self.__class__.__name__

    ###############################################################################
    # Zarr Store (MutableMapping) implementation
    ###############################################################################

    def keys(self) -> KeysView[str]:
        if self._trace_store_calls:
            print(f'{self._class_name}.keys()')
        return self._vfs.keys()

    def listdir(self, key: str) -> Iterable[str]:
        if self._trace_store_calls:
            print(f'{self._class_name}.listdir(key={key!r})')
        if key == '':
            return list((k for k in self._vfs.keys() if '/' not in k))
        else:
            prefix = key + '/'
            start = len(prefix)
            return list((k for k in self._vfs.keys() if k.startswith(prefix) and k.find('/', start) == -1))

    def getsize(self, key: str) -> int:
        if self._trace_store_calls:
            print(f'{self._class_name}.getsize(key={key!r})')
        return len(self._vfs[key])

    def __iter__(self) -> Iterator[str]:
        if self._trace_store_calls:
            print(f'{self._class_name}.__iter__()')
        return iter(self._vfs.keys())

    def __len__(self) -> int:
        if self._trace_store_calls:
            print(f'{self._class_name}.__len__()')
        return len(self._vfs.keys())

    def __contains__(self, key) -> bool:
        if self._trace_store_calls:
            print(f'{self._class_name}.__contains__(key={key!r})')
        return key in self._vfs

    def __getitem__(self, key: str) -> bytes:
        if self._trace_store_calls:
            print(f'{self._class_name}.__getitem__(key={key!r})')
        value = self._vfs[key]
        if isinstance(value, tuple):
            return self._fetch_chunk(*value)
        return value

    def __setitem__(self, key: str, value: bytes) -> None:
        if self._trace_store_calls:
            print(f'{self._class_name}.__setitem__(key={key!r}, value={value!r})')
        raise TypeError(f'{self._class_name} is read-only')

    def __delitem__(self, key: str) -> None:
        if self._trace_store_calls:
            print(f'{self._class_name}.__delitem__(key={key!r})')
        raise TypeError(f'{self._class_name} is read-only')


class CciStore(RemoteStore):
    """
    A remote Zarr Store using the ESA CCI Open Data Portal as backend.

    :param cci_odp: CCI ODP instance.
    :param cube_config: Cube configuration.
    :param observer: An optional callback function called when remote requests are mode: observer(**kwargs).
    :param trace_store_calls: Whether store calls shall be printed (for debugging).
    """

    _SAMPLE_TYPE_TO_DTYPE = {
        # Note: Sentinel Hub currently only supports unsigned
        # integer values therefore requesting INT8 or INT16
        # will return the same as UINT8 or UINT16 respectively.
        'uint8': '|u1',
        'uint16': '<u2',
        'uint32': '<u4',
        'int8': '|u1',
        'int16': '<u2',
        'int32': '<u4',
        'float32': '<f4',
        'float64': '<f8',
    }

    def __init__(self,
                 cci_odp: CciOdp,
                 cube_config: CubeConfig,
                 observer: Callable = None,
                 trace_store_calls=False):
        self._cci_odp = cci_odp
        self._metadata = None
        super().__init__(cube_config,
                         observer=observer,
                         trace_store_calls=trace_store_calls)

    def ensure_metadata_read(self):
        if self._metadata is None:
            self._metadata = self._cci_odp.get_dataset_metadata(self._cube_config.dataset_name)

    def get_time_range_for_num_days(self, num_days: int, start_time: datetime, end_time: datetime):
        temp_start_time = datetime(start_time.year, start_time.month, start_time.day)
        temp_start_time -= relativedelta(days=num_days - 1)
        temp_start_time = self._cci_odp.get_earliest_start_date(self.cube_config.dataset_name,
                                                                datetime.strftime(temp_start_time, _TIMESTAMP_FORMAT),
                                                                datetime.strftime(end_time, _TIMESTAMP_FORMAT),
                                                                f'{num_days} days')
        if temp_start_time:
            start_time = temp_start_time
        else:
            start_time = datetime(start_time.year, start_time.month, start_time.day)
        start_time_ordinal = start_time.toordinal()
        end_time_ordinal = end_time.toordinal()
        end_time_ordinal = start_time_ordinal + int(np.ceil((end_time_ordinal - start_time_ordinal) /
                                                            float(num_days)) * num_days)
        end_time = datetime.fromordinal(end_time_ordinal)
        end_time += relativedelta(days=1, microseconds=-1)
        delta = relativedelta(days=num_days, microseconds=-1)
        return start_time, end_time, delta

    def get_time_ranges(self) -> List[Tuple]:
        time_start, time_end = self._cube_config.time_range
        iso_start_time = time_start.tz_localize(None).isoformat()
        start_time = datetime.strptime(iso_start_time, _TIMESTAMP_FORMAT)
        iso_end_time = time_end.tz_localize(None).isoformat()
        end_time = datetime.strptime(iso_end_time, _TIMESTAMP_FORMAT)
        time_period = self.cube_config.dataset_name.split('.')[2]
        delta_ms = relativedelta(microseconds=1)
        if time_period == 'day':
            start_time = datetime(year=start_time.year, month=start_time.month, day=start_time.day)
            end_time = datetime(year=end_time.year, month=end_time.month, day=end_time.day)
            delta = relativedelta(days=1, microseconds=-1)
        elif time_period == 'month':
            start_time = datetime(year=start_time.year, month=start_time.month, day=1)
            end_time = datetime(year=end_time.year, month=end_time.month, day=1)
            delta = relativedelta(months=+1, microseconds=-1)
            end_time += delta
        elif time_period == 'year':
            start_time = datetime(year=start_time.year, month=1, day=1)
            end_time = datetime(year=end_time.year, month=12, day=31)
            delta = relativedelta(years=1, microseconds=-1)
        elif re.compile('[0-9]*-days').search(time_period):
            num_days = int(time_period.split('-')[0])
            temp_start_time = datetime(start_time.year, start_time.month, start_time.day)
            temp_start_time -= relativedelta(days=num_days - 1)
            temp_start_time = self._cci_odp.get_earliest_start_date(self.cube_config.dataset_name,
                                                                    datetime.strftime(temp_start_time,
                                                                                      _TIMESTAMP_FORMAT),
                                                                    iso_end_time,
                                                                    f'{num_days} days')
            if temp_start_time:
                start_time = temp_start_time
            else:
                start_time = datetime(start_time.year, start_time.month, start_time.day)
            start_time_ordinal = start_time.toordinal()
            end_time_ordinal = end_time.toordinal()
            end_time_ordinal = start_time_ordinal + int(np.ceil((end_time_ordinal - start_time_ordinal) /
                                                                float(num_days)) * num_days)
            end_time = datetime.fromordinal(end_time_ordinal)
            end_time += relativedelta(days=1, microseconds=-1)
            delta = relativedelta(days=num_days, microseconds=-1)
        else:
            # todo add support for satellite-orbit-frequency
            return []
        request_time_ranges = []
        this = start_time
        while this < end_time:
            next = this + delta
            pd_this = pd.Timestamp(datetime.strftime(this, _TIMESTAMP_FORMAT))
            pd_next = pd.Timestamp(datetime.strftime(next, _TIMESTAMP_FORMAT))
            request_time_ranges.append((pd_this, pd_next))
            this = next + delta_ms
        return request_time_ranges

    def get_spatial_lon_res(self):
        self.ensure_metadata_read()
        nc_attrs = self._metadata.get('attributes', {}).get('NC_GLOBAL', {})
        if 'geospatial_lon_resolution' in nc_attrs:
            return float(nc_attrs['geospatial_lon_resolution'])
        else:
            return float(nc_attrs['resolution'].split('x')[0].split('deg')[0])

    def get_spatial_lat_res(self):
        self.ensure_metadata_read()
        nc_attrs = self._metadata.get('attributes', {}).get('NC_GLOBAL', {})
        if 'geospatial_lat_resolution' in nc_attrs:
            return float(nc_attrs['geospatial_lat_resolution'])
        else:
            return float(nc_attrs['resolution'].split('x')[0].split('deg')[0])

    def get_dimension_data(self):
        self.ensure_metadata_read()
        dimension_names = self._metadata['dimensions']
        return self._cci_odp.get_dimension_data(self.cube_config.dataset_name, dimension_names)

    def get_encoding(self, var_name: str) -> Dict[str, Any]:
        self.ensure_metadata_read()
        encoding_dict = {}
        encoding_dict['fill_value'] = self._metadata.get('variable_infos', {}).get(var_name, {}).get('fill_value')
        encoding_dict['dtype'] = self._metadata.get('variable_infos', {}).get(var_name, {}).get('data_type')
        return encoding_dict

    def get_attrs(self, var_name: str) -> Dict[str, Any]:
        self.ensure_metadata_read()
        return self._metadata.get('variable_infos', {}).get(var_name, {})

    def fetch_chunk(self,
                    var_name: str,
                    chunk_index: Tuple[int, ...],
                    bbox: Tuple[float, float, float, float],
                    time_range: Tuple[pd.Timestamp, pd.Timestamp]) -> bytes:

        start_time, end_time = time_range
        if var_name == 'var_data':
            var_names = self.cube_config.variable_names
        else:
            var_names = [var_name]
        identifier = self._cci_odp.get_fid_for_dataset(self.cube_config.dataset_name)
        iso_start_date = start_time.tz_localize(None).isoformat()
        iso_end_date = end_time.tz_localize(None).isoformat()
        dim_indexes = self._get_dimension_indexes_for_request(var_names, bbox, iso_start_date, iso_end_date)
        request = dict(parentIdentifier=identifier,
                       varNames=var_names,
                       startDate=iso_start_date,
                       endDate=iso_end_date,
                       fileFormat='.nc'
                       )
        data = self._cci_odp.get_data(request, bbox, dim_indexes, self._dim_flipped)
        if not data:
            self.ensure_metadata_read()
            data = bytearray()
            for var_name in var_names:
                var_info = self._metadata.get('variable_infos', {}).get(var_name, {})
                var_dims = var_info.get('dimensions', [])
                length = 1
                for var_dim in var_dims:
                    dim_index = dim_indexes[var_dim]
                    if type(dim_index) == slice:
                        length *= (dim_index.stop - dim_index.start)
                dtype = np.dtype(self._SAMPLE_TYPE_TO_DTYPE[var_info['data_type']])
                var_array = np.full(shape=(length), fill_value=var_info['fill_value'], dtype=dtype)
                data += var_array.tobytes()
        return data

    def _get_dimension_indexes_for_request(self, var_names: List[str], bbox: Tuple[float, float, float, float],
                                           start_time: str, end_time: str):
        self.ensure_metadata_read()
        start_date = datetime.strptime(start_time, _TIMESTAMP_FORMAT)
        end_date = datetime.strptime(end_time, _TIMESTAMP_FORMAT)
        # todo support more dimensions
        supported_dimensions = ['lat', 'latitude', 'lon', 'longitude', 'time']
        dim_indexes = {}
        for var_name in var_names:
            affected_dimensions = self._metadata.get('variable_infos', {}).get(var_name, {}).get('dimensions', [])
            for dim in affected_dimensions:
                if dim not in supported_dimensions:
                    raise ValueError(f'Variable {var_name} has unsupported dimension {dim}. '
                                     f'Cannot retrieve this variable.')
                if dim not in dim_indexes:
                    dim_indexes[dim] = self._get_indexing(dim, bbox, start_date, end_date)
        return dim_indexes

    def _get_indexing(self, dimension: str, bbox: (float, float, float, float),
                      start_date: datetime, end_date: datetime):
        data = self._dimension_data[dimension]
        if dimension == 'lat' or dimension == 'latitude':
            return self._get_dim_indexing(dimension, data, bbox[1], bbox[3])
        if dimension == 'lon' or dimension == 'longitude':
            return self._get_dim_indexing(dimension, data, bbox[0], bbox[2])
        if dimension == 'time':
            return self._get_dim_indexing(dimension, data, start_date, end_date)

    def _get_dim_indexing(self, dimension_name, data, min, max):
        if len(data) == 1:
            return 0
        start_index = bisect.bisect_right(data, min)
        end_index = bisect.bisect_right(data, max)
        if self._dim_flipped[dimension_name]:
            temp_index = len(data) - start_index
            start_index = len(data) - end_index
            end_index = temp_index
        if start_index != end_index:
            return slice(start_index, end_index)
        return start_index
