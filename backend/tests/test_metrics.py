import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from mtw.metrics import Rates,process_sample,system_sample,HZ,PAGE,role_for
from mtw.monitoring import cached_snapshot,MAX_BYTES


class MetricTests(unittest.TestCase):
    def test_nspawn_payload_arguments_do_not_relabel_its_supervisor(self):
        words=['/usr/bin/systemd-nspawn','--machine=mtw-compute-3','/venv/bin/python','-m','mtw.service','compute-3']
        self.assertEqual(role_for(words,'container_runtime',True,7500),'container_runtime')
        self.assertEqual(role_for(['/venv/bin/python','-m','mtw.service','compute-3'],'container_runtime',False,4),'compute')
        self.assertEqual(role_for(['(sd-stubinit)'],'container_runtime',False,1),'container_init')
    def test_proc_numbers_match_kernel_fields_and_missing_pid_is_not_zero(self):
        p=process_sample(os.getpid());self.assertIsNotNone(p)
        raw=Path(f'/proc/{os.getpid()}/stat').read_text().rsplit(')',1)[1].split()
        self.assertEqual(p['rss_bytes'],int(raw[21])*PAGE)
        self.assertLessEqual(p['cpu_ticks'],int(raw[11])+int(raw[12]))
        self.assertEqual(p['host_pid'],os.getpid());self.assertIsNone(process_sample(999999999))
        system,_,_=system_sample();self.assertGreater(system['memory_total_bytes'],system['memory_available_bytes'])

    def test_rates_one_core_units_pid_reuse_and_first_sample_unknown(self):
        rate=Rates();p={'host_pid':10,'start_ticks':100,'cpu_ticks':2*HZ}
        system={};rate.apply([p],system,(1000,500),20,1)
        self.assertIsNone(p['cpu_percent']);self.assertIsNone(system.get('cpu_percent'))
        q=p|{'cpu_ticks':5*HZ};rate.apply([q],system,(1200,550),22,3)
        self.assertEqual(q['cpu_percent'],150);self.assertEqual(system['cpu_percent'],75)
        newer=p|{'start_ticks':101,'cpu_ticks':HZ};rate.apply([newer],{},(1300,600),24,5)
        self.assertIsNone(newer['cpu_percent'])

    def test_cache_age_boot_unavailable_and_size_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'snapshot.json'
            self.assertEqual(cached_snapshot(path,at=10,boot='test')['status'],'unavailable')
            path.write_text(json.dumps({'version':1,'sample_monotonic':10,'boot_id':'test'}))
            self.assertFalse(cached_snapshot(path,at=12,boot='test')['stale'])
            self.assertTrue(cached_snapshot(path,at=17,boot='test')['stale'])
            self.assertTrue(cached_snapshot(path,at=12,boot='reboot')['stale'])
            self.assertTrue(cached_snapshot(path,at=9,boot='test')['stale'])
            path.write_bytes(b'x'*(MAX_BYTES+1));self.assertEqual(cached_snapshot(path)['status'],'unavailable')
