import os
import unittest

from unittest import skipIf

from xcube_cci.cciodp import CciOdp
from xcube_cci.dataaccess import CciOdpDataOpener
from xcube_cci.dataaccess import CciOdpDataStore
from xcube.core.store.descriptor import DatasetDescriptor

class CciOdpDataOpenerTest(unittest.TestCase):

    def setUp(self) -> None:
        self.opener = CciOdpDataOpener(CciOdp())

    @skipIf(os.environ.get('XCUBE_DISABLE_WEB_TESTS', None) == '1', 'XCUBE_DISABLE_WEB_TESTS = 1')
    def test_describe_dataset(self):
        descriptor = self.opener.describe_data('esacci.OZONE.mon.L3.NP.multi-sensor.multi-platform.MERGED.fv0002.r1')
        self.assertIsNotNone(descriptor)
        self.assertIsInstance(descriptor, DatasetDescriptor)
        self.assertEqual('esacci.OZONE.mon.L3.NP.multi-sensor.multi-platform.MERGED.fv0002.r1', descriptor.data_id)
        self.assertEqual(['lon', 'lat', 'layers', 'air_pressure', 'time'], list(descriptor.dims.keys()))
        self.assertEqual(360, descriptor.dims['lon'])
        self.assertEqual(180, descriptor.dims['lat'])
        self.assertEqual(16, descriptor.dims['layers'])
        self.assertEqual(17, descriptor.dims['air_pressure'])
        self.assertEqual(1, descriptor.dims['time'])
        self.assertEqual(9, len(descriptor.data_vars))
        self.assertEqual('surface_pressure', descriptor.data_vars[0].name)
        self.assertEqual(3, descriptor.data_vars[0].ndim)
        self.assertEqual(('time', 'lat', 'lon'), descriptor.data_vars[0].dims)
        self.assertEqual('float32', descriptor.data_vars[0].dtype)
        self.assertIsNone(descriptor.crs)
        self.assertEqual(1.0, descriptor.spatial_res)
        self.assertEqual(('2000-02-01T00:00:00', '2014-12-31T23:59:59'), descriptor.time_range)
        self.assertEqual('1M', descriptor.time_period)

    @skipIf(os.environ.get('XCUBE_DISABLE_WEB_TESTS', None) == '1', 'XCUBE_DISABLE_WEB_TESTS = 1')
    def test_get_open_data_params_schema(self):
        schema = self.opener.get_open_data_params_schema(
            'esacci.OZONE.mon.L3.NP.multi-sensor.multi-platform.MERGED.fv0002.r1').to_dict()
        self.assertIsNotNone(schema)
        self.assertTrue('variable_names' in schema['properties'])
        self.assertTrue('time_range' in schema['properties'])

    @skipIf(os.environ.get('XCUBE_DISABLE_WEB_TESTS', None) == '1', 'XCUBE_DISABLE_WEB_TESTS = 1')
    def test_open_dataset(self):
        dataset = self.opener.open_data('esacci.OZONE.mon.L3.NP.multi-sensor.multi-platform.MERGED.fv0002.r1',
                                        variable_names=['surface_pressure', 'O3_du', 'O3e_du'],
                                        time_range=['2009-05-02', '2009-08-31'])
        self.assertIsNotNone(dataset)
        self.assertTrue('surface_pressure' in dataset.variables)
        self.assertTrue('O3_du' in dataset.variables)
        self.assertTrue('O3e_du' in dataset.variables)


class CciOdpDataStoreTest(unittest.TestCase):

    def setUp(self) -> None:
        self.store = CciOdpDataStore()

    def test_description(self):
        description = self.store.description
        self.assertIsNotNone(description)
        self.assertEqual('cciodp', description['store_id'])
        self.assertEqual('ESA CCI Open Data Portal', description['description'])
        import json
        with open('cci_store_datasets.json', 'w') as fp:
            json.dump(description, fp, indent=4)

    def test_get_data_store_params_schema(self):
        cci_store_params_schema = CciOdpDataStore.get_data_store_params_schema().to_dict()
        self.assertIsNotNone(cci_store_params_schema)
        self.assertTrue('opensearch_url' in cci_store_params_schema['properties'])
        self.assertTrue('opensearch_description_url' in cci_store_params_schema['properties'])

    def test_get_search_params(self):
        search_schema = self.store.get_search_params_schema().to_dict()
        self.assertIsNotNone(search_schema)
        self.assertTrue('start_date' in search_schema['properties'])
        self.assertTrue('end_date' in search_schema['properties'])
        self.assertTrue('bbox' in search_schema['properties'])
        self.assertTrue('ecv' in search_schema['properties'])
        self.assertTrue('frequency' in search_schema['properties'])
        self.assertTrue('institute' in search_schema['properties'])
        self.assertTrue('processing_level' in search_schema['properties'])
        self.assertTrue('product_string' in search_schema['properties'])
        self.assertTrue('product_version' in search_schema['properties'])
        self.assertTrue('data_type' in search_schema['properties'])
        self.assertTrue('sensor' in search_schema['properties'])
        self.assertTrue('platform' in search_schema['properties'])

    @skipIf(os.environ.get('XCUBE_DISABLE_WEB_TESTS', None) == '1', 'XCUBE_DISABLE_WEB_TESTS = 1')
    def test_search(self):
        search_result = list(self.store.search_data(ecv='FIRE', processing_level='L4', product_string='MODIS_TERRA'))
        self.assertIsNotNone(search_result)
        self.assertEqual(1, len(search_result))
        self.assertIsInstance(search_result[0], DatasetDescriptor)
        self.assertEqual(5, len(search_result[0].dims))
        self.assertEqual(6, len(search_result[0].data_vars))
        self.assertEqual('esacci.FIRE.mon.L4.BA.MODIS.Terra.MODIS_TERRA.v5-1.grid', search_result[0].data_id)
        self.assertEqual('31D', search_result[0].time_period)
        self.assertEqual(0.25, search_result[0].spatial_res)
        self.assertEqual('dataset', search_result[0].type_id)
        self.assertEqual(('2000-02-01T00:00:00', '2014-12-31T23:59:59'), search_result[0].time_range)

    @skipIf(os.environ.get('XCUBE_DISABLE_WEB_TESTS', None) == '1', 'XCUBE_DISABLE_WEB_TESTS = 1')
    def test_has_data(self):
        self.assertTrue(self.store.has_data('esacci.FIRE.mon.L4.BA.MODIS.Terra.MODIS_TERRA.v5-1.grid'))
        self.assertFalse(self.store.has_data('esacci.WIND.mon.L4.BA.MODIS.Terra.MODIS_TERRA.v5-1.grid'))

    @skipIf(os.environ.get('XCUBE_DISABLE_WEB_TESTS', None) == '1', 'XCUBE_DISABLE_WEB_TESTS = 1')
    def test_get_data_ids(self):
        dataset_ids_iter = self.store.get_data_ids()
        self.assertIsNotNone(dataset_ids_iter)
        dataset_ids = list(dataset_ids_iter)
        self.assertEqual(234, len(dataset_ids))

    def test_create_human_readable_title_from_id(self):
        self.assertEqual('OZONE CCI: Monthly multi-sensor L3 MERGED NP, vfv0002',
                         self.store._create_human_readable_title_from_id(
                             'esacci.OZONE.mon.L3.NP.multi-sensor.multi-platform.MERGED.fv0002.r1'))
        self.assertEqual('LC CCI: 13 year ASAR L4 Map WB, v4.0',
                         self.store._create_human_readable_title_from_id(
                             'esacci.LC.13-yrs.L4.WB.ASAR.Envisat.Map.4-0.r1'))
