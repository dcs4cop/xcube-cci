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
import re
import xarray as xr
import zarr

from xcube.core.store.dataaccess import DataAccessor
from xcube.core.store.dataaccess import DatasetDescriber
from xcube.core.store.dataaccess import GeoDataFrameOpener
from xcube.core.store.dataaccess import ZarrDatasetOpener
from xcube.core.store.descriptor import DatasetDescriptor
from xcube.core.store.descriptor import VariableDescriptor
from xcube.util.jsonschema import JsonArraySchema
from xcube.util.jsonschema import JsonIntegerSchema
from xcube.util.jsonschema import JsonNumberSchema
from xcube.util.jsonschema import JsonObjectSchema
from xcube.util.jsonschema import JsonStringSchema

from xcube_cci.cciodp import CciOdp
from xcube_cci.chunkstore import CciChunkStore
from xcube_cci.constants import DEFAULT_CRS

CRS_PATTERN = 'http://www.opengis.net/def/crs/EPSG/0/[0-9]{4,5}'
WKT_PATTERN = '[A-Z]*\(\([0-9 0-9,*]+\)\)'
TIME_PERIOD_PATTERN = '[0-9]+[Y|M|W|D|T|S|L|U|N|days|day|hours|hour|hr|h|minutes|minute|min|m|seconds|second|sec|' \
                      'milliseconds|millisecond|millis|milli|microseconds|microsecond|micros|micro|' \
                      'nanoseconds|nanosecond|nanos|nano|ns'

class ZarrCciOdpDatasetAccessor(DataAccessor, DatasetDescriber, ZarrDatasetOpener):

    def __init__(self):
        self._cci_odp = CciOdp()

    def describe_dataset(self, dataset_id: str) -> DatasetDescriptor:
        dataset_info = self._cci_odp.get_dataset_info(dataset_id)
        spatial_resolution = (dataset_info['lat_res'], dataset_info['lon_res'])
        spatial_coverage = dataset_info['bbox']
        temporal_coverage = (dataset_info['temporal_coverage_start'], dataset_info['temporal_coverage_end'])
        var_names = dataset_info['var_names']
        ds_metadata = self._cci_odp.get_dataset_metadata(dataset_id)
        var_descriptors = []
        var_infos = ds_metadata.get('variable_infos', {})
        for var_name in var_names:
            if var_name in var_infos:
                var_info = var_infos[var_name]
                var_dtype = var_info.pop('data_type')
                var_dims = var_info.pop('dimensions')
                var_descriptors.append(VariableDescriptor(var_name,
                                                          var_dtype,
                                                          var_dims,
                                                          var_info))
            else:
                var_descriptors.append(VariableDescriptor(var_name, '', ''))
        dims = ds_metadata['dims']
        attrs = ds_metadata.get('attributes', {}).get('NC_GLOBAL', {})
        temporal_resolution = attrs.get('time_coverage_resolution', '')[1:]
        if re.match(TIME_PERIOD_PATTERN, temporal_resolution) is None:
            temporal_resolution = None
        DatasetDescriptor(
            dataset_id=dataset_id,
            dims=dims,
            data_vars=var_descriptors,
            attrs=attrs,
            spatial_coverage=spatial_coverage,
            spatial_resolution=spatial_resolution,
            temporal_coverage=temporal_coverage,
            temporal_resolution=temporal_resolution
        )
        return DatasetDescriptor(dataset_id=dataset_id)

    def get_open_dataset_params_schema(self, dataset_id: str = None) -> JsonObjectSchema:
        dsd = self.describe_dataset(dataset_id) if dataset_id else None

        cube_params = dict(
            var_names=JsonArraySchema(items=JsonStringSchema(
                enum=[v.name for v in dsd.data_vars] if dsd and dsd.data_vars else None)),
            chunk_size=JsonArraySchema(items=JsonIntegerSchema()),
            time_range=JsonArraySchema(items=(JsonStringSchema(format='date-time'),
                                              JsonStringSchema(format='date-time')))
        )
        normalization_params = dict(
            bbox=JsonArraySchema(items=(JsonNumberSchema(),
                                        JsonNumberSchema(),
                                        JsonNumberSchema(),
                                        JsonNumberSchema())),
            geometry_wkt=JsonStringSchema(pattern=WKT_PATTERN),
            spatial_res=JsonNumberSchema(exclusive_minimum=0.0),
            crs=JsonStringSchema(pattern=CRS_PATTERN, default=DEFAULT_CRS),
            time_period=JsonStringSchema(pattern=TIME_PERIOD_PATTERN)
        )
        cci_schema = JsonObjectSchema(
            properties=dict(**cube_params,
                            **normalization_params
                            ),
            required=[
                # cube_params
                'var_names',
                'time_range',
            ],
            additional_properties=False
        )
        return cci_schema

    def open_dataset(self, dataset_id: str, **open_params) -> xr.Dataset:
        self.get_open_dataset_params_schema(dataset_id).validate_instance(open_params)

        max_cache_size: int = 2 ** 30
        cci_odp = CciOdp()
        cube_params = dict(
            var_names=open_params.pop('var_names'),
            chunk_size=open_params.pop('chunk_size', None),
            time_range=open_params.pop('time_range')
        )
        chunk_store = CciChunkStore(cci_odp, dataset_id, cube_params)
        if max_cache_size:
            chunk_store = zarr.LRUStoreCache(chunk_store, max_cache_size)
        raw_ds = xr.open_zarr(chunk_store)
        normalization_params = dict(
            bbox=open_params.pop('bbox', None),
            geometry_wkt=open_params.pop('geometry_wkt', None),
            spatial_res=open_params.pop('spatial_res'),
            crs=open_params.pop('crs', DEFAULT_CRS),
            time_period=open_params.pop('time_period', None)
        )
        return raw_ds
        # return cci_normalize(raw_ds, dataset_id, cube_params, cci_odp)


# class CciOdpGeoDataFrameOpener(GeoDataFrameOpener):
