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

import aiohttp
import asyncio
import bisect
import copy
import json
import logging
import lxml.etree as etree
import numpy as np
import os
import random
import re
import pandas as pd
import time
import urllib.parse
import warnings
from datetime import datetime
from dateutil.relativedelta import relativedelta
from typing import List, Dict, Tuple, Optional, Union, Sequence
from urllib.parse import quote
from xcube_cci.constants import CCI_ODD_URL
from xcube_cci.constants import DEFAULT_NUM_RETRIES
from xcube_cci.constants import DEFAULT_RETRY_BACKOFF_MAX
from xcube_cci.constants import DEFAULT_RETRY_BACKOFF_BASE
from xcube_cci.constants import OPENSEARCH_CEDA_URL
from xcube_cci.timeutil import get_timestrings_from_string

from pydap.client import Functions
from pydap.handlers.dap import BaseProxy
from pydap.handlers.dap import SequenceProxy
from pydap.handlers.dap import unpack_data
from pydap.lib import BytesReader
from pydap.lib import combine_slices
from pydap.lib import fix_slice
from pydap.lib import hyperslab
from pydap.lib import walk
from pydap.model import BaseType, SequenceType, GridType
from pydap.parsers import parse_ce
from pydap.parsers.dds import build_dataset
from pydap.parsers.das import parse_das, add_attributes
from six.moves.urllib.parse import urlsplit, urlunsplit

_LOG = logging.getLogger('xcube')
ODD_NS = {'os': 'http://a9.com/-/spec/opensearch/1.1/',
          'param': 'http://a9.com/-/spec/opensearch/extensions/parameters/1.0/'}
DESC_NS = {'gmd': 'http://www.isotc211.org/2005/gmd',
           'gml': 'http://www.opengis.net/gml/3.2',
           'gco': 'http://www.isotc211.org/2005/gco',
           'gmx': 'http://www.isotc211.org/2005/gmx',
           'xlink': 'http://www.w3.org/1999/xlink'
           }

_FEATURE_LIST_LOCK = asyncio.Lock()

_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S"

_RE_TO_DATETIME_FORMATS = \
    [(re.compile(14 * '\\d'), '%Y%m%d%H%M%S', relativedelta()),
     (re.compile(12 * '\\d'), '%Y%m%d%H%M', relativedelta(minutes=1, seconds=-1)),
     (re.compile(8 * '\\d'), '%Y%m%d', relativedelta(days=1, seconds=-1)),
     (re.compile(4 * '\\d' + '-' + 2 * '\\d' + '-' + 2 * '\\d'), '%Y-%m-%d',
      relativedelta(days=1, seconds=-1)),
     (re.compile(6 * '\\d'), '%Y%m', relativedelta(months=1, seconds=-1)),
     (re.compile(4 * '\\d'), '%Y', relativedelta(years=1, seconds=-1))]


def _convert_time_from_drs_id(time_value: str) -> str:
    if time_value == 'mon':
        return 'month'
    if time_value == 'yr':
        return 'year'
    if time_value == '5-days':
        return '5 days'
    if time_value == '8-days':
        return '8 days'
    if time_value == '15-days':
        return '15 days'
    if time_value == '13-yrs':
        return '13 years'
    return time_value


def _run_with_session(async_function, *params):
    # See https://github.com/aio-libs/aiohttp/blob/master/docs/client_advanced.rst#graceful-shutdown
    loop = asyncio.get_event_loop()
    coro = _run_with_session_executor(async_function, *params)
    result = loop.run_until_complete(coro)
    # Zero-sleep to allow underlying connections to close
    loop.run_until_complete(asyncio.sleep(0))
    return result


async def _run_with_session_executor(async_function, *params):
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=50)) as session:
        return await async_function(session, *params)


def _get_feature_dict_from_feature(feature: dict) -> Optional[dict]:
    fc_props = feature.get("properties", {})
    feature_dict = {'uuid': feature.get("id", "").split("=")[-1],
                    'title': fc_props.get("title", "")}
    variables = _get_variables_from_feature(feature)
    feature_dict['variables'] = variables
    fc_props_links = fc_props.get("links", None)
    if fc_props_links:
        search = fc_props_links.get("search", None)
        if search:
            odd_url = search[0].get('href', None)
            if odd_url:
                feature_dict['odd_url'] = odd_url
        described_by = fc_props_links.get("describedby", None)
        if described_by:
            for entry in described_by:
                if entry.get('title', '') == 'ISO19115':
                    metadata_url = entry.get("href", None)
                    if metadata_url:
                        feature_dict['metadata_url'] = metadata_url
                elif entry.get('title', '') == 'Dataset Information':
                    catalogue_url = entry.get("href", None)
                    if catalogue_url:
                        feature_dict['catalog_url'] = catalogue_url
    return feature_dict


def _get_variables_from_feature(feature: dict) -> List:
    feature_props = feature.get("properties", {})
    variables = feature_props.get("variables", [])
    variable_dicts = []
    for variable in variables:
        variable_dict = {
            'name': variable.get("var_id", None),
            'units': variable.get("units", ""),
            'long_name': variable.get("long_name", None)}
        variable_dicts.append(variable_dict)
    return variable_dicts


def _harmonize_info_field_names(catalogue: dict, single_field_name: str, multiple_fields_name: str,
                                multiple_items_name: Optional[str] = None):
    if single_field_name in catalogue and multiple_fields_name in catalogue:
        if len(multiple_fields_name) == 0:
            catalogue.pop(multiple_fields_name)
        elif len(catalogue[multiple_fields_name]) == 1:
            if catalogue[multiple_fields_name][0] is catalogue[single_field_name]:
                catalogue.pop(multiple_fields_name)
            else:
                catalogue[multiple_fields_name].append(catalogue[single_field_name])
                catalogue.pop(single_field_name)
        else:
            if catalogue[single_field_name] not in catalogue[multiple_fields_name] \
                    and (multiple_items_name is None
                         or catalogue[single_field_name] != multiple_items_name):
                catalogue[multiple_fields_name].append(catalogue[single_field_name])
            catalogue.pop(single_field_name)


def _extract_metadata_from_descxml(descxml: etree.XML) -> dict:
    metadata = {}
    metadata_elems = {
        'abstract': 'gmd:identificationInfo/gmd:MD_DataIdentification/gmd:abstract/'
                    'gco:CharacterString',
        'title': 'gmd:identificationInfo/gmd:MD_DataIdentification/gmd:citation/gmd:CI_Citation/'
                 'gmd:title/gco:CharacterString',
        'licences': 'gmd:identificationInfo/gmd:MD_DataIdentification/gmd:resourceConstraints/'
                    'gmd:MD_Constraints/gmd:useLimitation/gco:CharacterString',
        'bbox_minx': 'gmd:identificationInfo/gmd:MD_DataIdentification/gmd:extent/gmd:EX_Extent/'
                     'gmd:geographicElement/gmd:EX_GeographicBoundingBox/gmd:westBoundLongitude/'
                     'gco:Decimal',
        'bbox_miny': 'gmd:identificationInfo/gmd:MD_DataIdentification/gmd:extent/gmd:EX_Extent/'
                     'gmd:geographicElement/gmd:EX_GeographicBoundingBox/gmd:southBoundLatitude/'
                     'gco:Decimal',
        'bbox_maxx': 'gmd:identificationInfo/gmd:MD_DataIdentification/gmd:extent/gmd:EX_Extent/'
                     'gmd:geographicElement/gmd:EX_GeographicBoundingBox/gmd:eastBoundLongitude/'
                     'gco:Decimal',
        'bbox_maxy': 'gmd:identificationInfo/gmd:MD_DataIdentification/gmd:extent/gmd:EX_Extent/'
                     'gmd:geographicElement/gmd:EX_GeographicBoundingBox/gmd:northBoundLatitude/'
                     'gco:Decimal',
        'temporal_coverage_start': 'gmd:identificationInfo/gmd:MD_DataIdentification/gmd:extent/'
                                   'gmd:EX_Extent/gmd:temporalElement/gmd:EX_TemporalExtent/'
                                   'gmd:extent/gml:TimePeriod/gml:beginPosition',
        'temporal_coverage_end': 'gmd:identificationInfo/gmd:MD_DataIdentification/gmd:extent/'
                                 'gmd:EX_Extent/gmd:temporalElement/gmd:EX_TemporalExtent/'
                                 'gmd:extent/gml:TimePeriod/gml:endPosition'
    }
    for identifier in metadata_elems:
        content = _get_element_content(descxml, metadata_elems[identifier])
        if content:
            metadata[identifier] = content
    metadata_elems_with_replacement = {'file_formats': [
        'gmd:identificationInfo/gmd:MD_DataIdentification/gmd:resourceFormat/gmd:MD_Format/'
        'gmd:name/gco:CharacterString', 'Data are in NetCDF format', '.nc']
    }
    for metadata_elem in metadata_elems_with_replacement:
        content = \
            _get_replaced_content_from_descxml_elem(descxml,
                                                    metadata_elems_with_replacement[metadata_elem])
        if content:
            metadata[metadata_elem] = content
    metadata_linked_elems = {
        'publication_date': ['gmd:identificationInfo/gmd:MD_DataIdentification/gmd:citation/'
                             'gmd:CI_Citation/gmd:date/gmd:CI_Date/gmd:dateType/'
                             'gmd:CI_DateTypeCode', 'publication', '../../gmd:date/gco:DateTime'],
        'creation_date': ['gmd:identificationInfo/gmd:MD_DataIdentification/gmd:citation/'
                          'gmd:CI_Citation/gmd:date/gmd:CI_Date/gmd:dateType/gmd:CI_DateTypeCode',
                          'creation', '../../gmd:date/gco:DateTime']
    }
    for identifier in metadata_linked_elems:
        content = _get_linked_content_from_descxml_elem(descxml, metadata_linked_elems[identifier])
        if content:
            metadata[identifier] = content
    return metadata


def _get_element_content(descxml: etree.XML, path: str) -> Optional[Union[str, List[str]]]:
    elems = descxml.findall(path, namespaces=DESC_NS)
    if not elems:
        return None
    if len(elems) == 1:
        return elems[0].text
    return [elem.text for elem in elems]


def _get_replaced_content_from_descxml_elem(descxml: etree.XML, paths: List[str]) -> Optional[str]:
    descxml_elem = descxml.find(paths[0], namespaces=DESC_NS)
    if descxml_elem is None:
        return None
    if descxml_elem.text == paths[1]:
        return paths[2]


def _get_linked_content_from_descxml_elem(descxml: etree.XML, paths: List[str]) -> Optional[str]:
    descxml_elems = descxml.findall(paths[0], namespaces=DESC_NS)
    if descxml is None:
        return None
    for descxml_elem in descxml_elems:
        if descxml_elem.text == paths[1]:
            return _get_element_content(descxml_elem, paths[2])


def find_datetime_format(filename: str) -> Tuple[Optional[str], int, int, relativedelta]:
    for regex, time_format, timedelta in _RE_TO_DATETIME_FORMATS:
        searcher = regex.search(filename)
        if searcher:
            p1, p2 = searcher.span()
            return time_format, p1, p2, timedelta
    return None, -1, -1, relativedelta()


def _extract_metadata_from_odd(odd_xml: etree.XML) -> dict:
    metadata = {'num_files': {}}
    metadata_names = {'ecv': (['ecv', 'ecvs'], False),
                      'frequency': (['time_frequency', 'time_frequencies'], False),
                      'institute': (['institute', 'institutes'], False),
                      'processingLevel': (['processing_level', 'processing_levels'], False),
                      'productString': (['product_string', 'product_strings'], False),
                      'productVersion': (['product_version', 'product_versions'], False),
                      'dataType': (['data_type', 'data_types'], False),
                      'sensor': (['sensor_id', 'sensor_ids'], False),
                      'platform': (['platform_id', 'platform_ids'], False),
                      'fileFormat': (['file_format', 'file_formats'], False),
                      'drsId': (['drs_id', 'drs_ids'], True)}
    for param_elem in odd_xml.findall('os:Url/param:Parameter', namespaces=ODD_NS):
        if param_elem.attrib['name'] in metadata_names:
            element_names, add_to_num_files = metadata_names[param_elem.attrib['name']]
            param_content = _get_from_param_elem(param_elem)
            if param_content:
                if type(param_content) == tuple:
                    metadata[element_names[0]] = param_content[0]
                    if add_to_num_files:
                        metadata['num_files'][param_content[0]] = param_content[1]
                else:
                    names = []
                    for name, num_files in param_content:
                        names.append(name)
                        if add_to_num_files:
                            metadata['num_files'][name] = num_files
                    metadata[element_names[1]] = names
    return metadata


def _get_from_param_elem(param_elem: etree.Element):
    options = param_elem.findall('param:Option', namespaces=ODD_NS)
    if not options:
        return None
    if len(options) == 1:
        return options[0].get('value'), int(options[0].get('label').split('(')[-1][:-1])
    return [(option.get('value'), int(option.get('label').split('(')[-1][:-1]))
            for option in options]


def _extract_feature_info(feature: dict) -> List:
    feature_props = feature.get("properties", {})
    filename = feature_props.get("title", "")
    date = feature_props.get("date", None)
    start_time = ""
    end_time = ""
    if date and "/" in date:
        start_time, end_time = date.split("/")
    elif filename:
        time_format, p1, p2, timedelta = find_datetime_format(filename)
        if time_format:
            start_time = datetime.strptime(filename[p1:p2], time_format)
            end_time = start_time + timedelta
            # Convert back to text, so we can JSON-encode it
            start_time = datetime.strftime(start_time, _TIMESTAMP_FORMAT)
            end_time = datetime.strftime(end_time, _TIMESTAMP_FORMAT)
    file_size = feature_props.get("filesize", 0)
    related_links = feature_props.get("links", {}).get("related", [])
    urls = {}
    for related_link in related_links:
        urls[related_link.get("title")] = related_link.get("href")
    return [filename, start_time, end_time, file_size, urls]


def _get_res(nc_attrs: dict, dim: str) -> float:
    if dim == 'lat':
        attr_name = 'geospatial_lat_resolution'
        index = 0
    else:
        attr_name = 'geospatial_lon_resolution'
        index = -1
    for name in [attr_name, 'resolution', 'spatial_resolution']:
        if name in nc_attrs:
            res_attr = nc_attrs[name]
            try:
                if type(res_attr) == float:
                    return res_attr
                elif type(res_attr) == int:
                    return float(res_attr)
                # as we now expect to deal with a string, we try to parse a float
                # for that, we remove any trailing units and consider that
                # lat and lon might be given, separated by an 'x'
                return float(res_attr.split('(')[0].split('x')[index].split('deg')[0].
                             split('degree')[0].split('km')[0].split('m')[0])
            except ValueError:
                continue
    return -1.0


class CciOdp:
    """
    Represents the ESA CCI Open Data Portal

    :param opensearch_url: The base URL to the opensearch service
    :param opensearch_description_url: The URL to a document describing
    the capabilities of the opensearch service
    """

    def __init__(self,
                 opensearch_url: str = OPENSEARCH_CEDA_URL,
                 opensearch_description_url: str = CCI_ODD_URL,
                 enable_warnings: bool = False,
                 num_retries: int = DEFAULT_NUM_RETRIES,
                 retry_backoff_max: int = DEFAULT_RETRY_BACKOFF_MAX,
                 retry_backoff_base: float = DEFAULT_RETRY_BACKOFF_BASE,
                 only_consider_cube_ready = False
                 ):
        self._opensearch_url = opensearch_url
        self._opensearch_description_url = opensearch_description_url
        self._enable_warnings = enable_warnings
        self._num_retries = num_retries
        self._retry_backoff_max = retry_backoff_max
        self._retry_backoff_base = retry_backoff_base
        self._drs_ids = None
        self._data_sources = {}
        self._features = {}
        self._datasets = {}
        eds_file = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'data/excluded_data_sources')
        with open(eds_file, 'r') as eds:
            self._excluded_data_sources = eds.read().split('\n')
        if only_consider_cube_ready:
            ncds_file = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     'data/non_cube_data_sources')
            with open(ncds_file, 'r') as ncds:
                non_cube_ready_data_sources = ncds.read().split('\n')
                self._excluded_data_sources += non_cube_ready_data_sources

    def close(self):
        pass

    @property
    def dataset_names(self) -> List[str]:
        return _run_with_session(self._fetch_dataset_names)

    def get_dataset_info(self, dataset_id: str, dataset_metadata: dict = None) -> dict:
        data_info = {}
        if not dataset_metadata:
            dataset_metadata = self.get_dataset_metadata(dataset_id)
        nc_attrs = dataset_metadata.get('attributes', {}).get('NC_GLOBAL', {})
        data_info['lat_res'] = _get_res(nc_attrs, 'lat')
        data_info['lon_res'] = _get_res(nc_attrs, 'lon')
        data_info['bbox'] = (float(dataset_metadata['bbox_minx']),
                             float(dataset_metadata['bbox_miny']),
                             float(dataset_metadata['bbox_maxx']),
                             float(dataset_metadata['bbox_maxy']))
        data_info['temporal_coverage_start'] = dataset_metadata.get('temporal_coverage_start', '')
        data_info['temporal_coverage_end'] = dataset_metadata.get('temporal_coverage_end', '')
        data_info['var_names'] = self.var_names(dataset_id)
        return data_info

    def get_dataset_metadata(self, dataset_id: str) -> dict:
        return self.get_datasets_metadata([dataset_id])[0]

    def get_datasets_metadata(self, dataset_ids: List[str]) -> List[dict]:
        assert isinstance(dataset_ids, list)
        _run_with_session(self._ensure_all_info_in_data_sources, dataset_ids)
        metadata = []
        for dataset_id in dataset_ids:
            metadata.append(self._data_sources[dataset_id])
        return metadata

    async def _fetch_dataset_names(self, session):
        if self._drs_ids:
            return self._drs_ids
        meta_info_dict = \
            await self._extract_metadata_from_odd_url(session, self._opensearch_description_url)
        if 'drs_ids' in meta_info_dict:
            self._drs_ids = meta_info_dict['drs_ids']
            for excluded_data_source in self._excluded_data_sources:
                if excluded_data_source in self._drs_ids:
                    self._drs_ids.remove(excluded_data_source)
            return self._drs_ids
        if not self._data_sources:
            self._data_sources = {}
            catalogue = await self._fetch_data_source_list_json(session,
                                                                self._opensearch_url,
                                                                dict(parentIdentifier='cci'))
            if catalogue:
                tasks = []
                for catalogue_item in catalogue:
                    tasks.append(self._create_data_source(session,
                                                          catalogue[catalogue_item],
                                                          catalogue_item))
                await asyncio.gather(*tasks)
        return list(self._data_sources.keys())

    async def _create_data_source(self, session, json_dict: dict, datasource_id: str):
        meta_info = await self._fetch_meta_info(session,
                                                json_dict.get('odd_url', None),
                                                json_dict.get('metadata_url', None))
        drs_ids = self._get_as_list(meta_info, 'drs_id', 'drs_ids')
        for excluded_data_source in self._excluded_data_sources:
            if excluded_data_source in drs_ids:
                    drs_ids.remove(excluded_data_source)
        for drs_id in drs_ids:
            drs_meta_info = copy.deepcopy(meta_info)
            drs_meta_info.update(json_dict)
            self._adjust_json_dict(drs_meta_info, drs_id)
            for variable in drs_meta_info.get('variables', []):
                variable['name'] = variable['name'].replace('.', '_')
            drs_meta_info['cci_project'] = drs_meta_info['ecv']
            drs_meta_info['fid'] = datasource_id
            drs_meta_info['num_files'] = drs_meta_info['num_files'][drs_id]
            self._data_sources[drs_id] = drs_meta_info

    def _adjust_json_dict(self, json_dict: dict, drs_id: str):
        values = drs_id.split('.')
        self._adjust_json_dict_for_param(json_dict, 'time_frequency', 'time_frequencies',
                                         _convert_time_from_drs_id(values[2]))
        self._adjust_json_dict_for_param(json_dict, 'processing_level', 'processing_levels',
                                         values[3])
        self._adjust_json_dict_for_param(json_dict, 'data_type', 'data_types', values[4])
        self._adjust_json_dict_for_param(json_dict, 'sensor_id', 'sensor_ids', values[5])
        self._adjust_json_dict_for_param(json_dict, 'platform_id', 'platform_ids', values[6])
        self._adjust_json_dict_for_param(json_dict, 'product_string', 'product_strings',
                                         values[7])
        self._adjust_json_dict_for_param(json_dict, 'product_version', 'product_versions',
                                         values[8])

    @staticmethod
    def _adjust_json_dict_for_param(json_dict: dict, single_name: str, list_name: str,
                                    param_value: str):
        json_dict[single_name] = param_value
        if list_name in json_dict:
            json_dict.pop(list_name)

    @staticmethod
    def _get_as_list(meta_info: dict, single_name: str, list_name: str) -> List:
        if single_name in meta_info:
            return [meta_info[single_name]]
        if list_name in meta_info:
            return meta_info[list_name]
        return []

    def var_names(self, dataset_name: str) -> List:
        _run_with_session(self._ensure_all_info_in_data_sources, [dataset_name])
        return self._get_data_var_names(self._data_sources[dataset_name]['variable_infos'])

    async def _ensure_all_info_in_data_sources(self, session, dataset_names: List[str]):
        await self._ensure_in_data_sources(session, dataset_names)
        all_info_tasks = []
        for dataset_name in dataset_names:
            all_info_tasks.append(self._ensure_all_info_in_data_source(session, dataset_name))
        await asyncio.gather(*all_info_tasks)

    async def _ensure_all_info_in_data_source(self, session, dataset_name: str):
        data_source = self._data_sources[dataset_name]
        if 'dimensions' in data_source \
                and 'variable_infos' in data_source \
                and 'attributes' in data_source:
            return
        data_fid = await self._get_fid_for_dataset(session, dataset_name)
        await self._set_variable_infos(self._opensearch_url, data_fid, dataset_name, session,
                                       data_source)

    @staticmethod
    def _get_data_var_names(variable_infos) -> List:
        variables = []
        names_of_dims = ['period', 'hist1d_cla_vis006_bin_centre', 'lon_bnds', 'air_pressure',
                         'field_name_length', 'lon', 'view', 'hist2d_cot_bin_centre',
                         'hist1d_cer_bin_border', 'altitude', 'vegetation_class',
                         'hist1d_cla_vis006_bin_border', 'time_bnds', 'hist1d_ctp_bin_border',
                         'hist1d_cot_bin_centre', 'hist1d_cot_bin_border',
                         'hist1d_cla_vis008_bin_centre', 'lat_bnds', 'hist1d_cwp_bin_border',
                         'layers', 'hist1d_cer_bin_centre', 'aerosol_type',
                         'hist1d_ctt_bin_border', 'hist1d_ctp_bin_centre', 'fieldsp1', 'time',
                         'hist_phase', 'hist1d_cwp_bin_centre', 'hist2d_ctp_bin_border', 'lat',
                         'fields', 'hist2d_cot_bin_border', 'hist2d_ctp_bin_centre',
                         'hist1d_ctt_bin_centre', 'hist1d_cla_vis008_bin_border', 'crs']
        for variable in variable_infos:
            if variable in names_of_dims:
                continue
            if len(variable_infos[variable]['dimensions']) == 0:
                continue
            if variable_infos[variable].get('data_type', '') not in \
                    ['uint8', 'uint16', 'uint32', 'int8', 'int16', 'int32', 'float32', 'float64']:
                continue
            variables.append(variable)
        return variables

    def search(self,
               start_date: Optional[str] = None,
               end_date: Optional[str] = None,
               bbox: Optional[Tuple[float, float, float, float]] = None,
               ecv: Optional[str] = None,
               frequency: Optional[str] = None,
               institute: Optional[str] = None,
               processing_level: Optional[str] = None,
               product_string: Optional[str] = None,
               product_version: Optional[str] = None,
               data_type: Optional[str] = None,
               sensor: Optional[str] = None,
               platform: Optional[str] = None) -> List[str]:
        candidate_names = []
        if not self._data_sources and not ecv and not frequency and not processing_level \
                and not data_type and not product_string and not product_version:
            _run_with_session(self._read_all_data_sources)
            candidate_names = self.dataset_names
        else:
            for dataset_name in self.dataset_names:
                split_dataset_name = dataset_name.split('.')
                if ecv is not None and ecv != split_dataset_name[1]:
                    continue
                if frequency is not None and frequency != \
                        _convert_time_from_drs_id(split_dataset_name[2]):
                    continue
                if processing_level is not None and processing_level != split_dataset_name[3]:
                    continue
                if data_type is not None and data_type != split_dataset_name[4]:
                    continue
                if product_string is not None and product_string != split_dataset_name[7]:
                    continue
                if product_version is not None and product_version.replace('.', '-') != \
                        split_dataset_name[8]:
                    continue
                candidate_names.append(dataset_name)
            if len(candidate_names) == 0:
                return []
        if not start_date and not end_date and not institute and not sensor and not platform \
                and not bbox:
            return candidate_names
        results = []
        if start_date:
            converted_start_date = self._get_datetime_from_string(start_date)
        if end_date:
            converted_end_date = self._get_datetime_from_string(end_date)
        _run_with_session(self._ensure_in_data_sources, candidate_names)
        for candidate_name in candidate_names:
            data_source_info = self._data_sources.get(candidate_name, None)
            if not data_source_info:
                continue
            if institute is not None and ('institute' not in data_source_info or
                                          institute != data_source_info['institute']):
                continue
            if sensor is not None and sensor != data_source_info['sensor_id']:
                continue
            if platform is not None and platform != data_source_info['platform_id']:
                continue
            if bbox:
                if float(data_source_info['bbox_minx']) > bbox[2]:
                    continue
                if float(data_source_info['bbox_maxx']) < bbox[0]:
                    continue
                if float(data_source_info['bbox_miny']) > bbox[3]:
                    continue
                if float(data_source_info['bbox_maxy']) < bbox[1]:
                    continue
            if start_date:
                data_source_end = datetime.strptime(data_source_info['temporal_coverage_end'],
                                                    _TIMESTAMP_FORMAT)
                # noinspection PyUnboundLocalVariable
                if converted_start_date > data_source_end:
                    continue
            if end_date:
                data_source_start = datetime.strptime(data_source_info['temporal_coverage_start'],
                                                      _TIMESTAMP_FORMAT)
                # noinspection PyUnboundLocalVariable
                if converted_end_date < data_source_start:
                    continue
            results.append(candidate_name)
        return results

    async def _read_all_data_sources(self, session):
        catalogue = await self._fetch_data_source_list_json(session,
                                                       self._opensearch_url,
                                                       dict(parentIdentifier='cci'))
        if catalogue:
            tasks = []
            for catalogue_item in catalogue:
                tasks.append(self._create_data_source(session,
                                                      catalogue[catalogue_item],
                                                      catalogue_item))
            await asyncio.gather(*tasks)

    async def _ensure_in_data_sources(self, session, dataset_names: List[str]):
        dataset_names_to_check = []
        for dataset_name in dataset_names:
            if dataset_name not in self._data_sources:
                dataset_names_to_check.append(dataset_name)
        if len(dataset_names_to_check) == 0:
            return
        fetch_fid_tasks = []
        catalogue = {}
        for dataset_name in dataset_names_to_check:
            fetch_fid_tasks.append(
                self._update_catalogue_with_data_source_list(session, catalogue, dataset_name)
            )
        await asyncio.gather(*fetch_fid_tasks)
        create_source_tasks = []
        for catalogue_item in catalogue:
            create_source_tasks.append(self._create_data_source(session,
                                                                catalogue[catalogue_item],
                                                                catalogue_item))
        await asyncio.gather(*create_source_tasks)

    async def _update_catalogue_with_data_source_list(self,
                                                      session,
                                                      catalogue: dict,
                                                      dataset_name: str):
        dataset_catalogue = await self._fetch_data_source_list_json(session,
                                                                    self._opensearch_url,
                                                                    dict(parentIdentifier='cci',
                                                                    drsId=dataset_name))
        catalogue.update(dataset_catalogue)

    @staticmethod
    def _get_datetime_from_string(time_as_string: str) -> datetime:
        time_format, start, end, timedelta = find_datetime_format(time_as_string)
        return datetime.strptime(time_as_string[start:end], time_format)

    def get_variable_data(self, dataset_name: str,
                          dimension_names: Dict[str, int],
                          start_time: str = '1900-01-01T00:00:00',
                          end_time: str = '3001-12-31T00:00:00'):
        dimension_data = _run_with_session(self._get_var_data, dataset_name, dimension_names,
                                           start_time, end_time)
        return dimension_data

    async def _get_var_data(self, session, dataset_name: str, variable_names: Dict[str, int],
                            start_time: str, end_time: str):
        fid = await self._get_fid_for_dataset(session, dataset_name)
        request = dict(parentIdentifier=fid,
                       startDate=start_time,
                       endDate=end_time,
                       drsId=dataset_name
                       )
        opendap_url = await self._get_opendap_url(session, request)
        var_data = {}
        if not opendap_url:
            return var_data
        dataset = await self._get_opendap_dataset(session, opendap_url)
        if not dataset:
            return var_data
        for var_name in variable_names:
            if var_name in dataset:
                var_data[var_name] = \
                    dict(size=dataset[var_name].size,
                         chunkSize=dataset[var_name].attributes.get('_ChunkSizes'))
                if dataset[var_name].size < 512 * 512:
                    data = await self._get_data_from_opendap_dataset(dataset, session, var_name,
                                                                     (slice(None, None, None),))
                    if data is None:
                        var_data[var_name]['data'] = []
                    else:
                        var_data[var_name]['data'] = data
                else:
                    var_data[var_name]['data'] = []
            else:
                var_data[var_name] = dict(size=variable_names[var_name],
                                          chunkSize=variable_names[var_name],
                                          data=list(range(variable_names[var_name])))
        return var_data

    async def _get_feature_list(self, session, request):
        ds_id = request['drsId']
        start_date_str = request['startDate']
        start_date = datetime.strptime(start_date_str, _TIMESTAMP_FORMAT)
        end_date_str = request['endDate']
        end_date = datetime.strptime(end_date_str, _TIMESTAMP_FORMAT)
        feature_list = []
        if ds_id not in self._features or len(self._features[ds_id]) == 0:
            self._features[ds_id] = []
            await self._fetch_opensearch_feature_list(session, self._opensearch_url, feature_list,
                                                      self._extract_times_and_opendap_url, request)
            if len(feature_list) == 0:
                # try without dates. For some data sets, this works better
                request.pop('startDate')
                request.pop('endDate')
                await self._fetch_opensearch_feature_list(session,
                                                          self._opensearch_url,
                                                          feature_list,
                                                          self._extract_times_and_opendap_url,
                                                          request)
            feature_list.sort(key=lambda x: x[0])
            self._features[ds_id] = feature_list
        else:
            if start_date < self._features[ds_id][0][0]:
                request['endDate'] = datetime.strftime(self._features[ds_id][0][0],
                                                       _TIMESTAMP_FORMAT)
                await self._fetch_opensearch_feature_list(session,
                                                          self._opensearch_url,
                                                          feature_list,
                                                          self._extract_times_and_opendap_url,
                                                          request)
                feature_list.sort(key=lambda x: x[0])
                self._features[ds_id] = feature_list + self._features[ds_id]
            if end_date > self._features[ds_id][-1][1]:
                request['startDate'] = datetime.strftime(self._features[ds_id][-1][1],
                                                         _TIMESTAMP_FORMAT)
                request['endDate'] = end_date_str
                await self._fetch_opensearch_feature_list(session,
                                                          self._opensearch_url,
                                                          feature_list,
                                                          self._extract_times_and_opendap_url,
                                                          request)
                feature_list.sort(key=lambda x: x[0])
                self._features[ds_id] = self._features[ds_id] + feature_list
        start = bisect.bisect_left([feature[1] for feature in self._features[ds_id]], start_date)
        end = bisect.bisect_right([feature[0] for feature in self._features[ds_id]], end_date)
        return self._features[ds_id][start:end]

    @staticmethod
    def _extract_times_and_opendap_url(features: List[Tuple], feature_list: List[Dict]):
        for feature in feature_list:
            start_time = None
            end_time = None
            properties = feature.get('properties', {})
            opendap_url = None
            links = properties.get('links', {}).get('related', {})
            for link in links:
                if link.get('title', '') == 'Opendap':
                    opendap_url = link.get('href', None)
            if not opendap_url:
                continue
            date_property = properties.get('date', None)
            if date_property:
                split_date = date_property.split('/')
                # remove trailing symbols from times
                start_time = datetime.strptime(split_date[0].split('.')[0].split('+')[0],
                                               _TIMESTAMP_FORMAT)
                end_time = datetime.strptime(split_date[1].split('.')[0].split('+')[0],
                                             _TIMESTAMP_FORMAT)
            else:
                title = properties.get('title', None)
                if title:
                    start_time, end_time = get_timestrings_from_string(title)
                    if start_time:
                        start_time = datetime.strptime(start_time, _TIMESTAMP_FORMAT)
                    if end_time:
                        end_time = datetime.strptime(end_time, _TIMESTAMP_FORMAT)
                    else:
                        end_time = start_time
            if start_time:
                start_time = pd.Timestamp(datetime.strftime(start_time, _TIMESTAMP_FORMAT))
                end_time = pd.Timestamp(datetime.strftime(end_time, _TIMESTAMP_FORMAT))
                features.append((start_time, end_time, opendap_url))

    def get_time_ranges_from_data(self, dataset_name: str, start_time: str, end_time: str) -> \
            List[Tuple[datetime, datetime]]:
        return _run_with_session(self._get_time_ranges_from_data,
                                 dataset_name,
                                 start_time,
                                 end_time)

    async def _get_time_ranges_from_data(self, session, dataset_name: str, start_time: str,
                                         end_time: str) -> List[Tuple[datetime, datetime]]:
        fid = await self._get_fid_for_dataset(session, dataset_name)
        request = dict(parentIdentifier=fid,
                       startDate=start_time,
                       endDate=end_time,
                       drsId=dataset_name,
                       fileFormat='.nc')

        feature_list = await self._get_feature_list(session, request)
        request_time_ranges = [feature[0:2] for feature in feature_list]
        return request_time_ranges

    def get_file_list(self, dataset_name):
        return _run_with_session(self._get_file_list, dataset_name)

    async def _get_file_list(self, session, dataset_name):
        fid = await self._get_fid_for_dataset(session, dataset_name)
        request = dict(parentIdentifier=fid,
                       drsId=dataset_name,
                       startDate='1900-01-01T00:00:00',
                       endDate='3001-12-31T00:00:00',
                       fileFormat='.nc')
        feature_list = await self._get_feature_list(session, request)
        file_list = [feature[2] for feature in feature_list]
        return file_list

    def get_fid_for_dataset(self, dataset_name: str) -> str:
        return _run_with_session(self._get_fid_for_dataset, dataset_name)

    async def _get_fid_for_dataset(self, session, dataset_name: str) -> str:
        await self._ensure_in_data_sources(session, [dataset_name])
        return self._data_sources[dataset_name]['fid']

    async def _get_opendap_url(self, session, request: Dict):
        request['fileFormat'] = '.nc'
        # async with _FEATURE_LIST_LOCK:
        feature_list = await self._get_feature_list(session, request)
        if len(feature_list) == 0:
            return
        return feature_list[0][2]

    def get_data_chunk(self, request: Dict, dim_indexes: Tuple) -> Optional[bytes]:
        data_chunk = _run_with_session(self._get_data_chunk, request, dim_indexes)
        return data_chunk

    async def _get_data_chunk(self, session, request: Dict, dim_indexes: Tuple) -> Optional[bytes]:
        var_name = request['varNames'][0]
        opendap_url = await self._get_opendap_url(session, request)
        if not opendap_url:
            return None
        dataset = await self._get_opendap_dataset(session, opendap_url)
        if not dataset:
            return None
        data = await self._get_data_from_opendap_dataset(dataset, session, var_name, dim_indexes)
        variable_data = np.array(data, dtype=dataset[var_name].dtype.type)
        result = variable_data.flatten().tobytes()
        return result

    async def _fetch_data_source_list_json(self, session, base_url, query_args) -> Sequence:
        def _extender(inner_catalogue: dict, feature_list: List[Dict]):
            for fc in feature_list:
                fc_props = fc.get("properties", {})
                fc_id = fc_props.get("identifier", None)
                if not fc_id:
                    continue
                inner_catalogue[fc_id] = _get_feature_dict_from_feature(fc)
        catalogue = {}
        await self._fetch_opensearch_feature_list(session, base_url, catalogue, _extender,
                                                  query_args)
        return catalogue

    async def _fetch_opensearch_feature_list(self, session, base_url, extension, extender,
                                             query_args):
        """
        Return JSON value read from Opensearch web service.
        :return:
        """
        start_page = 1
        initial_maximum_records = 1000
        maximum_records = 10000
        total_results = await self._fetch_opensearch_feature_part_list(session, base_url,
                                                                       query_args, start_page,
                                                                       initial_maximum_records,
                                                                       extension, extender,
                                                                       None, None)
        if total_results < initial_maximum_records:
            return
        # num_results = maximum_records
        num_results = 0
        extension.clear()
        while num_results < total_results:
            if 'startDate' in query_args and 'endDate' in query_args:
                # we have to clear the extension of any previous values to avoid duplicate values
                # extension.clear()
                start_time = datetime.strptime(query_args.pop('startDate'), _TIMESTAMP_FORMAT)
                end_time = datetime.strptime(query_args.pop('endDate'), _TIMESTAMP_FORMAT)
                num_days_per_delta = \
                    int(np.ceil((end_time - start_time).days / (total_results / 1000)))
                delta = relativedelta(days=num_days_per_delta, seconds=-1)
                tasks = []
                current_time = start_time
                while current_time < end_time:
                    task_start = current_time.strftime(_TIMESTAMP_FORMAT)
                    current_time += delta
                    if current_time > end_time:
                        current_time = end_time
                    task_end = current_time.strftime(_TIMESTAMP_FORMAT)
                    tasks.append(self._fetch_opensearch_feature_part_list(session, base_url,
                                                                          query_args, start_page,
                                                                          maximum_records,
                                                                          extension,
                                                                          extender,
                                                                          task_start, task_end))
                await asyncio.gather(*tasks)
                num_results = total_results
            else:
                tasks = []
                # do not have more than 4 open connections at the same time
                while len(tasks) < 4 and num_results < total_results:
                    tasks.append(self._fetch_opensearch_feature_part_list(session, base_url,
                                                                          query_args, start_page,
                                                                          maximum_records,
                                                                          extension,
                                                                          extender, None, None))
                    start_page += 1
                    num_results += maximum_records
                await asyncio.gather(*tasks)

    async def _fetch_opensearch_feature_part_list(self, session, base_url, query_args, start_page,
                                                  maximum_records, extension, extender,
                                                  start_date, end_date) -> int:
        paging_query_args = dict(query_args or {})
        paging_query_args.update(startPage=start_page, maximumRecords=maximum_records,
                                 httpAccept='application/geo+json')
        if start_date:
            paging_query_args.update(startDate=start_date)
        if end_date:
            paging_query_args.update(endDate=end_date)
        url = base_url + '?' + urllib.parse.urlencode(paging_query_args)
        resp = await self.get_response(session, url)
        if resp:
            json_text = await resp.read()
            json_dict = json.loads(json_text.decode('utf-8'))
            if extender:
                feature_list = json_dict.get("features", [])
                extender(extension, feature_list)
            return json_dict['totalResults']
        if 'startDate' in paging_query_args and 'endDate' in paging_query_args:
            _LOG.debug(f'Did not read page {start_page} with start date '
                       f'{paging_query_args["startDate"]} and '
                       f'end date {paging_query_args["endDate"]}')
        else:
            _LOG.debug(f'Did not read page {start_page}')
        return 0

    async def _set_variable_infos(self, opensearch_url: str, dataset_id: str,
                                  dataset_name: str, session, data_source):
        attributes = {}
        dimensions = {}
        variable_infos = {}
        feature, time_dimension_size = \
            await self._fetch_feature_and_num_nc_files_at(session,
                                                          opensearch_url,
                                                          dict(parentIdentifier=dataset_id,
                                                               drsId=dataset_name),
                                                          1)
        time_dimension_size = data_source['num_files']
        if feature is not None:
            variable_infos, attributes = \
                await self._get_variable_infos_from_feature(feature, session)
            for variable_info in variable_infos:
                for index, dimension in enumerate(variable_infos[variable_info]['dimensions']):
                    if not dimension in dimensions:
                        dimensions[dimension] = variable_infos[variable_info]['shape'][index]
            if 'time' in dimensions:
                time_dimension_size *= dimensions['time']
        data_source['dimensions'] = dimensions
        data_source['variable_infos'] = variable_infos
        data_source['attributes'] = attributes
        data_source['time_dimension_size'] = time_dimension_size

    async def _fetch_feature_and_num_nc_files_at(self, session, base_url, query_args, index) -> \
            Tuple[Optional[Dict], int]:
        paging_query_args = dict(query_args or {})
        paging_query_args.update(startPage=index,
                                 maximumRecords=2,
                                 httpAccept='application/geo+json',
                                 fileFormat='.nc')
        url = base_url + '?' + urllib.parse.urlencode(paging_query_args)
        resp = await self.get_response(session, url)
        if resp:
            json_text = await resp.read()
            json_dict = json.loads(json_text.decode('utf-8'))
            feature_list = json_dict.get("features", [])
            # we try not to take the first feature, as the last and the first one may have
            # different time chunkings
            if len(feature_list) > 1:
                return feature_list[1], json_dict.get("totalResults", 0)
            elif len(feature_list) > 0:
                return feature_list[0], json_dict.get("totalResults", 0)
        return None, 0

    async def _fetch_meta_info(self, session, odd_url: str, metadata_url: str) -> Dict:
        meta_info_dict = {}
        if odd_url:
            meta_info_dict = await self._extract_metadata_from_odd_url(session, odd_url)
        if metadata_url:
            desc_metadata = await self._extract_metadata_from_descxml_url(session, metadata_url)
            for item in desc_metadata:
                if not item in meta_info_dict:
                    meta_info_dict[item] = desc_metadata[item]
        _harmonize_info_field_names(meta_info_dict, 'file_format', 'file_formats')
        _harmonize_info_field_names(meta_info_dict, 'platform_id', 'platform_ids')
        _harmonize_info_field_names(meta_info_dict, 'sensor_id', 'sensor_ids')
        _harmonize_info_field_names(meta_info_dict, 'processing_level', 'processing_levels')
        _harmonize_info_field_names(meta_info_dict, 'time_frequency', 'time_frequencies')
        return meta_info_dict

    async def _extract_metadata_from_descxml_url(self, session, descxml_url: str = None) -> dict:
        if not descxml_url:
            return {}
        resp = await self.get_response(session, descxml_url)
        if resp:
            descxml = etree.XML(await resp.read())
            try:
                return _extract_metadata_from_descxml(descxml)
            except etree.ParseError:
                _LOG.info(f'Cannot read metadata from {descxml_url} due to parsing error.')
        return {}

    async def _extract_metadata_from_odd_url(self, session: aiohttp.ClientSession,
                                             odd_url: str = None) -> dict:
        if not odd_url:
            return {}
        resp = await self.get_response(session, odd_url)
        if not resp:
            return {}
        xml_text = await resp.read()
        return _extract_metadata_from_odd(etree.XML(xml_text))

    async def _get_variable_infos_from_feature(self, feature: dict, session) -> (dict, dict):
        feature_info = _extract_feature_info(feature)
        opendap_url = f"{feature_info[4]['Opendap']}"
        dataset = await self._get_opendap_dataset(session, opendap_url)
        if not dataset:
            _LOG.warning(f'Could not extract information about variables and attributes '
                         f'from {opendap_url}')
            return {}, {}
        variable_infos = {}
        for key in dataset.keys():
            fixed_key = key.replace('%2E', '_').replace('.', '_')
            variable_infos[fixed_key] = copy.deepcopy(dataset[key].attributes)
            if '_FillValue' in variable_infos[fixed_key]:
                variable_infos[fixed_key]['fill_value'] = variable_infos[fixed_key]['_FillValue']
                variable_infos[fixed_key].pop('_FillValue')
            if '_ChunkSizes' in variable_infos[fixed_key]:
                variable_infos[fixed_key]['chunk_sizes'] = variable_infos[fixed_key]['_ChunkSizes']
                if type(variable_infos[fixed_key]['chunk_sizes']) == int:
                    variable_infos[fixed_key]['file_chunk_sizes'] = \
                        variable_infos[fixed_key]['chunk_sizes']
                else:
                    variable_infos[fixed_key]['file_chunk_sizes'] = \
                        copy.deepcopy(variable_infos[fixed_key]['chunk_sizes'])
                variable_infos[fixed_key].pop('_ChunkSizes')
            variable_infos[fixed_key]['data_type'] = dataset[key].dtype.name
            variable_infos[fixed_key]['dimensions'] = list(dataset[key].dimensions)
            variable_infos[fixed_key]['file_dimensions'] = \
                copy.deepcopy(variable_infos[fixed_key]['dimensions'])
            variable_infos[fixed_key]['size'] = dataset[key].size
            variable_infos[fixed_key]['shape'] = list(dataset[key].shape)
        return variable_infos, dataset.attributes

    def get_opendap_dataset(self, url: str):
        return _run_with_session(self._get_opendap_dataset, url)

    async def _get_opendap_dataset(self, session, url: str):
        if url in self._datasets:
            return self._datasets[url]
        tasks = []
        res_dict = {}
        tasks.append(self._get_content_from_opendap_url(url, 'dds', res_dict, session))
        tasks.append(self._get_content_from_opendap_url(url, 'das', res_dict, session))
        await asyncio.gather(*tasks)
        if 'dds' not in res_dict or 'das' not in res_dict:
            _LOG.warning('Could not open opendap url. No dds or das file provided.')
            return
        if res_dict['dds'] == '':
            _LOG.warning('Could not open opendap url. dds file is empty.')
            return
        dataset = build_dataset(res_dict['dds'])
        res_dict['das'] = res_dict['das'].replace('        Float32 valid_min -Infinity;\n', '')
        res_dict['das'] = res_dict['das'].replace('        Float32 valid_max Infinity;\n', '')
        add_attributes(dataset, parse_das(res_dict['das']))

        # remove any projection from the url, leaving selections
        scheme, netloc, path, query, fragment = urlsplit(url)
        projection, selection = parse_ce(query)
        url = urlunsplit((scheme, netloc, path, '&'.join(selection), fragment))

        # now add data proxies
        for var in walk(dataset, BaseType):
            var.data = BaseProxy(url, var.id, var.dtype, var.shape)
        for var in walk(dataset, SequenceType):
            template = copy.copy(var)
            var.data = SequenceProxy(url, template)

        # apply projections
        for var in projection:
            target = dataset
            while var:
                token, index = var.pop(0)
                target = target[token]
                if isinstance(target, BaseType):
                    target.data.slice = fix_slice(index, target.shape)
                elif isinstance(target, GridType):
                    index = fix_slice(index, target.array.shape)
                    target.array.data.slice = index
                    for s, child in zip(index, target.maps):
                        target[child].data.slice = (s,)
                elif isinstance(target, SequenceType):
                    target.data.slice = index

        # retrieve only main variable for grid types:
        for var in walk(dataset, GridType):
            var.set_output_grid(True)

        dataset.functions = Functions(url)
        self._datasets[url] = dataset
        return dataset

    async def _get_content_from_opendap_url(self, url: str, part: str, res_dict: dict, session):
        scheme, netloc, path, query, fragment = urlsplit(url)
        url = urlunsplit((scheme, netloc, path + f'.{part}', query, fragment))
        resp = await self.get_response(session, url)
        if resp:
            res_dict[part] = await resp.read()
            res_dict[part] = str(res_dict[part], 'utf-8')

    async def _get_data_from_opendap_dataset(self, dataset, session, variable_name, slices):
        proxy = dataset[variable_name].data
        if type(proxy) == list:
            proxy = proxy[0]
        # build download url
        index = combine_slices(proxy.slice, fix_slice(slices, proxy.shape))
        scheme, netloc, path, query, fragment = urlsplit(proxy.baseurl)
        url = urlunsplit((
            scheme, netloc, path + '.dods',
            quote(proxy.id) + hyperslab(index) + '&' + query,
            fragment)).rstrip('&')
        # download and unpack data
        resp = await self.get_response(session, url)
        if not resp:
            return None
        content = await resp.read()
        dds, data = content.split(b'\nData:\n', 1)
        dds = str(dds, 'utf-8')
        # Parse received dataset:
        dataset = build_dataset(dds)
        dataset.data = unpack_data(BytesReader(data), dataset)
        return dataset[proxy.id].data

    async def get_response(self, session: aiohttp.ClientSession, url: str) -> \
            Optional[aiohttp.ClientResponse]:
        num_retries = self._num_retries
        retry_backoff_max = self._retry_backoff_max  # ms
        retry_backoff_base = self._retry_backoff_base
        response = None
        for i in range(num_retries):
            resp = await session.request(method='GET', url=url)
            if resp.status == 200:
                return resp
            elif 500 <= resp.status < 600:
                # Retry (immediately) on 5xx errors
                continue
            elif resp.status == 429:
                # Retry after 'Retry-After' with exponential backoff
                retry_min = int(response.headers.get('Retry-After', '100'))
                retry_backoff = random.random() * retry_backoff_max
                retry_total = retry_min + retry_backoff
                if self._enable_warnings:
                    retry_message = f'Error 429: Too Many Requests. ' \
                                    f'Attempt {i + 1} of {num_retries} to retry after ' \
                                    f'{"%.2f" % retry_min} + {"%.2f" % retry_backoff} = ' \
                                    f'{"%.2f" % retry_total} ms...'
                    warnings.warn(retry_message)
                time.sleep(retry_total / 1000.0)
                retry_backoff_max *= retry_backoff_base
            else:
                break
        return None
