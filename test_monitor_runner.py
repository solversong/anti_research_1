"""Integration using invented transport only. These are not model experiments."""
import json
from pathlib import Path
import tempfile
import unittest
from test_runner import inputs, Renderer, Stub
import run_draft_pilot as runner
import pilot_support as support
from monitor import read_snapshot, assess
from operational import exclusive_run

class MonitoringIntegration(unittest.TestCase):
    def test_prefix_and_resume_distinguish_new_requests(self):
        d,r,c=inputs()
        with tempfile.TemporaryDirectory() as folder:
            a=Path(folder)/'a';b=Path(folder)/'b'
            runner.run(d,r,c,'synthetic_spec',a,True,2,transport_factory=Stub,renderer_factory=Renderer)
            p=read_snapshot(a)
            self.assertEqual((p['status'],p['total'],p['completed'],p['new_attempts']),('planned_prefix_complete',2,2,2))
            m=runner.run(d,r,c,'synthetic_spec',b,True,resume=a,transport_factory=Stub,renderer_factory=Renderer)
            p=read_snapshot(b)
            self.assertEqual((p['status'],p['total'],p['completed'],p['reused_records']),('completed',22,22,2))
            self.assertEqual(m['cumulative_generation_request_attempts'],24)
            self.assertNotIn('progress.json',m['outputs_sha256'])
            self.assertNotIn('events.jsonl',m['outputs_sha256'])
            self.assertIn('code_provenance',m)
            for n,h in m['outputs_sha256'].items():self.assertEqual(support.file_sha(b/n),h)

    def test_dry_run_does_not_construct_transport(self):
        d,r,c=inputs()
        with tempfile.TemporaryDirectory() as folder:
            out=Path(folder)/'dry'
            runner.run(d,r,c,'synthetic_spec',out,limit=2,transport_factory=lambda _:self.fail('network'),renderer_factory=Renderer)
            p=read_snapshot(out)
            self.assertEqual((p['status'],p['completed'],p['new_attempts']),('dry_run_complete',0,0))

    def test_three_errors_are_failed_not_completed(self):
        d,r,c=inputs()
        with tempfile.TemporaryDirectory() as folder:
            out=Path(folder)/'fail'
            runner.run(d,r,c,'synthetic_spec',out,True,transport_factory=lambda x:Stub(x,True),renderer_factory=Renderer)
            p=read_snapshot(out)
            self.assertEqual((p['status'],p['completed'],p['failed'],p['new_attempts']),('failed',3,3,3))

    def test_interruption_preserves_unknown_request(self):
        class Interrupted(Stub):
            def request(self,route,payload=None):
                if route=='/api/generate':
                    self.generation_requests+=1;self.network_requests+=1
                    raise KeyboardInterrupt
                return super().request(route,payload)
        d,r,c=inputs()
        with tempfile.TemporaryDirectory() as folder:
            out=Path(folder)/'interrupt'
            with self.assertRaises(KeyboardInterrupt):
                runner.run(d,r,c,'synthetic_spec',out,True,2,transport_factory=Interrupted,renderer_factory=Renderer)
            p=read_snapshot(out);m=json.loads((out/'run_manifest.json').read_text())
            self.assertEqual((p['status'],p['new_attempts'],p['completed'],p['outcome_unrecorded']),('interrupted',1,0,1))
            self.assertEqual(m['generation_outcome_unrecorded_requests'],1)
            self.assertTrue(m['scores_withheld'])

    def test_unexpected_exception_cannot_report_success(self):
        class Broken(Stub):
            def request(self,route,payload=None):
                if route=='/api/generate':
                    self.generation_requests+=1;self.network_requests+=1
                    raise RuntimeError('PRIVATE_SENTINEL')
                return super().request(route,payload)
        d,r,c=inputs()
        with tempfile.TemporaryDirectory() as folder:
            out=Path(folder)/'broken'
            with self.assertRaises(RuntimeError):
                runner.run(d,r,c,'synthetic_spec',out,True,2,transport_factory=Broken,renderer_factory=Renderer)
            self.assertEqual(read_snapshot(out)['status'],'failed')
            m=json.loads((out/'run_manifest.json').read_text())
            self.assertEqual(m['status'],'ABORTED_UNEXPECTED_ERROR')
            for n in ('progress.json','events.jsonl','run_manifest.json'):
                self.assertNotIn('PRIVATE_SENTINEL',(out/n).read_text())

    def test_preflight_overflow_visible_with_no_model_call(self):
        class TooLong(Renderer):
            def render(self,m):return {'prompt':'invented','prompt_tokens':99999}
        d,r,c=inputs()
        with tempfile.TemporaryDirectory() as folder:
            out=Path(folder)/'overflow'
            with self.assertRaises(support.ContractError):
                runner.run(d,r,c,'synthetic_spec',out,True,2,transport_factory=lambda _:self.fail('network'),renderer_factory=TooLong)
            self.assertEqual((read_snapshot(out)['status'],read_snapshot(out)['new_attempts']),('failed',0))

    def test_shared_output_root_prevents_parallel_execution(self):
        d,r,c=inputs()
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            with exclusive_run(root):
                with self.assertRaisesRegex(RuntimeError,'RUN_ALREADY_ACTIVE'):
                    runner.run(d,r,c,'synthetic_spec',root/'duplicate',True,2,transport_factory=Stub,renderer_factory=Renderer)
            self.assertFalse((root/'duplicate').exists())

if __name__=='__main__':unittest.main()
