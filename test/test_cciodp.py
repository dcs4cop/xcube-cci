import logging
import os
import unittest

from xcube_cci.cciodp import _retrieve_attribute_info_from_das, CciOdp

class CciOdpTest(unittest.TestCase):

    def test_retrieve_attribute_info_from_das(self):
        das_file = os.path.join(os.path.dirname(__file__),
                                "resources/ESACCI-OZONE-L3-NP-MERGED-KNMI-199701-fv0002.nc.das")
        das = open(das_file)
        das_read = das.read()
        attribute_info = _retrieve_attribute_info_from_das(das_read)
        self.assertEqual(15, len(attribute_info))
        self.assertTrue('surface_pressure' in attribute_info)
        self.assertTrue('fill_value' in attribute_info['surface_pressure'])
        self.assertEqual('NaN', attribute_info['surface_pressure']['fill_value'])

    def test_get_data(self):
        cci_odp = CciOdp()
        request = dict(parentIdentifier='4eb4e801424a47f7b77434291921f889',
                       startDate='1997-05-01T00:00:00',
                       endDate='1997-05-01T00:00:00',
                       varNames=['surface_pressure', 'O3e_du_tot']
                       )
        bbox = (-10.0, 40.0, 10.0, 60.0)
        data = cci_odp.get_data(request, bbox, {})
        self.assertIsNotNone(data)

    def test_dataset_names(self):
        cci_odp = CciOdp()
        dataset_names = cci_odp.dataset_names
        self.assertIsNotNone(dataset_names)

    def test_var_names(self):
        cci_odp = CciOdp()
        var_names = cci_odp.var_names('esacci.OC.8-days.L3S.OC_PRODUCTS.multi-sensor.multi-platform.MERGED.4-0.geographic')
        self.assertIsNotNone(var_names)
        self.assertEqual(['Rrs_412', 'Rrs_443', 'Rrs_490', 'Rrs_510', 'Rrs_555', 'Rrs_670', 'water_class1',
                          'water_class2', 'water_class3', 'water_class4', 'water_class5', 'water_class6',
                          'water_class7', 'water_class8', 'water_class9', 'water_class10', 'water_class11',
                          'water_class12', 'water_class13', 'water_class14', 'atot_412', 'atot_443', 'atot_490',
                          'atot_510', 'atot_555', 'atot_670', 'aph_412', 'aph_443', 'aph_490', 'aph_510', 'aph_555',
                          'aph_670', 'adg_412', 'adg_443', 'adg_490', 'adg_510', 'adg_555', 'adg_670', 'bbp_412',
                          'bbp_443', 'bbp_490', 'bbp_510', 'bbp_555', 'bbp_670', 'chlor_a', 'kd_490', 'Rrs_412_rmsd',
                          'Rrs_443_rmsd', 'Rrs_490_rmsd', 'Rrs_510_rmsd', 'Rrs_555_rmsd', 'Rrs_670_rmsd',
                          'Rrs_412_bias', 'Rrs_443_bias', 'Rrs_490_bias', 'Rrs_510_bias', 'Rrs_555_bias',
                          'Rrs_670_bias', 'chlor_a_log10_rmsd', 'chlor_a_log10_bias', 'aph_412_rmsd', 'aph_443_rmsd',
                          'aph_490_rmsd', 'aph_510_rmsd', 'aph_555_rmsd', 'aph_670_rmsd', 'aph_412_bias',
                          'aph_443_bias', 'aph_490_bias', 'aph_510_bias', 'aph_555_bias', 'aph_670_bias',
                          'adg_412_rmsd', 'adg_443_rmsd', 'adg_490_rmsd', 'adg_510_rmsd', 'adg_555_rmsd',
                          'adg_670_rmsd', 'adg_412_bias', 'adg_443_bias', 'adg_490_bias', 'adg_510_bias',
                          'adg_555_bias', 'adg_670_bias', 'kd_490_rmsd', 'kd_490_bias', 'SeaWiFS_nobs_sum',
                          'MODISA_nobs_sum', 'MERIS_nobs_sum', 'VIIRS_nobs_sum', 'total_nobs_sum'], var_names)

    def test_get_dataset_info(self):
        cci_odp = CciOdp()
        dataset_info = cci_odp.get_dataset_info('esacci.CLOUD.month.L3C.CLD_PRODUCTS.MODIS.Terra.MODIS_TERRA.2-0.r1')
        self.assertIsNotNone(dataset_info)
        self.assertTrue('lat_res' in dataset_info)
        self.assertEqual(0.5, dataset_info['lat_res'])
        self.assertTrue('lon_res' in dataset_info)
        self.assertEqual(0.5, dataset_info['lon_res'])
        self.assertTrue('bbox' in dataset_info)
        self.assertEqual((-180.0, -90.0, 180.0, 90.0), dataset_info['bbox'])
        self.assertTrue('var_names' in dataset_info)
        self.assertEqual(['nobs', 'nobs_day', 'nobs_clear_day', 'nobs_cloudy_day', 'nobs_clear_night',
                          'nobs_cloudy_night', 'nobs_clear_twl', 'nobs_cloudy_twl', 'nobs_cloudy', 'nretr_cloudy',
                          'nretr_cloudy_liq', 'nretr_cloudy_ice', 'nretr_cloudy_day', 'nretr_cloudy_day_liq',
                          'nretr_cloudy_day_ice', 'nretr_cloudy_low', 'nretr_cloudy_mid', 'nretr_cloudy_high', 'cfc',
                          'cfc_std', 'cfc_prop_unc', 'cfc_corr_unc', 'cfc_unc', 'cfc_low', 'cfc_mid', 'cfc_high',
                          'cfc_day', 'cfc_night', 'cfc_twl', 'ctt', 'ctt_std', 'ctt_prop_unc', 'ctt_corr_unc',
                          'ctt_unc', 'ctt_corrected', 'ctt_corrected_std', 'ctt_corrected_prop_unc',
                          'ctt_corrected_corr_unc', 'ctt_corrected_unc', 'stemp', 'stemp_std', 'stemp_prop_unc',
                          'stemp_corr_unc', 'stemp_unc', 'cth', 'cth_std', 'cth_prop_unc', 'cth_corr_unc', 'cth_unc',
                          'cth_corrected', 'cth_corrected_std', 'cth_corrected_prop_unc', 'cth_corrected_corr_unc',
                          'cth_corrected_unc', 'ctp', 'ctp_std', 'ctp_prop_unc', 'ctp_corr_unc', 'ctp_unc', 'ctp_log',
                          'ctp_corrected', 'ctp_corrected_std', 'ctp_corrected_prop_unc', 'ctp_corrected_corr_unc',
                          'ctp_corrected_unc', 'cph', 'cph_std', 'cph_day', 'cph_day_std', 'cer', 'cer_std',
                          'cer_prop_unc', 'cer_corr_unc', 'cer_unc', 'cot', 'cot_log', 'cot_std', 'cot_prop_unc',
                          'cot_corr_unc', 'cot_unc', 'cee', 'cee_std', 'cee_prop_unc', 'cee_corr_unc', 'cee_unc',
                          'cla_vis006', 'cla_vis006_std', 'cla_vis006_prop_unc', 'cla_vis006_corr_unc',
                          'cla_vis006_unc', 'cla_vis006_liq', 'cla_vis006_liq_std', 'cla_vis006_liq_unc',
                          'cla_vis006_ice', 'cla_vis006_ice_std', 'cla_vis006_ice_unc', 'cla_vis008', 'cla_vis008_std',
                          'cla_vis008_prop_unc', 'cla_vis008_corr_unc', 'cla_vis008_unc', 'cla_vis008_liq',
                          'cla_vis008_liq_std', 'cla_vis008_liq_unc', 'cla_vis008_ice', 'cla_vis008_ice_std',
                          'cla_vis008_ice_unc', 'lwp', 'lwp_std', 'lwp_prop_unc', 'lwp_corr_unc', 'lwp_unc',
                          'lwp_allsky', 'iwp', 'iwp_std', 'iwp_prop_unc', 'iwp_corr_unc', 'iwp_unc', 'iwp_allsky',
                          'cer_liq', 'cer_liq_std', 'cer_liq_prop_unc', 'cer_liq_corr_unc', 'cer_liq_unc', 'cer_ice',
                          'cer_ice_std', 'cer_ice_prop_unc', 'cer_ice_corr_unc', 'cer_ice_unc', 'cot_liq',
                          'cot_liq_std', 'cot_liq_prop_unc', 'cot_liq_corr_unc', 'cot_liq_unc', 'cot_ice',
                          'cot_ice_std', 'cot_ice_prop_unc', 'cot_ice_corr_unc', 'cot_ice_unc', 'hist2d_cot_ctp',
                          'hist1d_cot', 'hist1d_ctp', 'hist1d_ctt', 'hist1d_cer', 'hist1d_cwp', 'hist1d_cla_vis006',
                          'hist1d_cla_vis008'], dataset_info['var_names'])
        self.assertTrue('temporal_coverage_start' in dataset_info)
        self.assertEqual('2000-02-01T00:00:00', dataset_info['temporal_coverage_start'])
        self.assertTrue('temporal_coverage_end' in dataset_info)
        self.assertEqual('2014-12-31T23:59:59', dataset_info['temporal_coverage_end'])
