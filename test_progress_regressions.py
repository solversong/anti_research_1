"""Independent content-free monitor and runtime audit; no real transport/models."""
import contextlib, copy, io, json, os, pathlib, sys, tempfile, time, unittest
from unittest.mock import patch
DEFAULT=pathlib.Path(__file__).resolve().parent
if not (DEFAULT/'run_draft_pilot.py').is_file(): DEFAULT=DEFAULT/'repo'
R=pathlib.Path(os.environ.get('ANTIBIOTIC_AUDIT_REPO',str(DEFAULT)))
sys.path.insert(0,str(R))
import run_draft_pilot as runner
from test_runner import inputs, Renderer, Stub
import progress, monitor

class ProgressIndependentAudit(unittest.TestCase):
 def make(self,p,total=2,**kwargs):return progress.Progress(p,model='invented-model',version='audit-v1',total=total,heartbeat_seconds=.02,**kwargs)
 def test_terminal_not_clobbered_by_heartbeat(self):
  with tempfile.TemporaryDirectory() as td:
   p=self.make(pathlib.Path(td)/'r',1);p.start_item(1);time.sleep(.055);p.finish_item(success=True);last=p.finish('completed');time.sleep(.055)
   disk=monitor.read_snapshot(pathlib.Path(td)/'r');self.assertEqual(disk['status'],'completed');self.assertEqual(disk['sequence'],last['sequence'])
 def test_reader_rejects_forged_completed_counter(self):
  with tempfile.TemporaryDirectory() as td:
   p=self.make(pathlib.Path(td)/'r');v=p.snapshot();p.finish('interrupted');v.update(status='completed',stage='finished')
   with self.assertRaises(monitor.InvalidProgress):monitor.validate_snapshot(v)
 def test_metadata_extension_or_escape_rejected_without_display(self):
  with tempfile.TemporaryDirectory() as td:
   p=self.make(pathlib.Path(td)/'r');v=p.snapshot();p.finish('interrupted')
   for key,value in [('rationale_short','PRIVATE_SENTINEL'),('model','bad\x1b[31mPRIVATE_SENTINEL')]:
    item=copy.deepcopy(v);item[key]=value;stream=io.StringIO()
    with contextlib.redirect_stdout(stream),self.assertRaises(monitor.InvalidProgress):monitor.render_snapshot(item)
    self.assertNotIn('PRIVATE_SENTINEL',stream.getvalue())
 def test_prefix_reuse_not_new_request(self):
  with tempfile.TemporaryDirectory() as td:
   p=self.make(pathlib.Path(td)/'r',22,dataset_total=24,reused_records=2);p.start_item(1)
   v=p.snapshot();self.assertEqual((v['new_attempts'],v['completed'],v['current_dataset_ordinal']),(1,0,3));p.finish('interrupted')
   self.assertEqual(monitor.read_snapshot(pathlib.Path(td)/'r')['outcome_unrecorded'],1)
 def test_stale_and_absent_pid_are_not_model_health(self):
  with tempfile.TemporaryDirectory() as td:
   p=self.make(pathlib.Path(td)/'r');v=p.snapshot();p.finish('interrupted')
   a=monitor.assess(v,now=v['heartbeat_at_unix']+100,process_probe=lambda _: 'exists')
   b=monitor.assess(v,now=v['heartbeat_at_unix'],process_probe=lambda _: 'absent')
   self.assertTrue(a['heartbeat_stale']);self.assertEqual(a['model_health'],'not_inferred');self.assertIn('absent',b['observation'])
 def test_finished_record_does_not_require_existing_pid(self):
  with tempfile.TemporaryDirectory() as td:
   p=self.make(pathlib.Path(td)/'r',0);v=p.finish('completed')
   a=monitor.assess(v,now=v['heartbeat_at_unix']+100,process_probe=lambda _:self.fail('terminal must not probe PID'))
   self.assertEqual(a['observation'],'terminal_recorded');self.assertFalse(a['heartbeat_stale'])
 def test_special_fifo_rejected_without_blocking(self):
  with tempfile.TemporaryDirectory() as td:
   p=pathlib.Path(td);os.mkfifo(p/'progress.json')
   with self.assertRaises(monitor.InvalidProgress):monitor.read_snapshot(p)
 def test_second_writer_and_existing_run_rejected(self):
  with tempfile.TemporaryDirectory() as td:
   d=pathlib.Path(td)/'r';p=self.make(d)
   with self.assertRaises(RuntimeError):self.make(d)
   p.finish('interrupted')
   with self.assertRaises(FileExistsError):self.make(d)

class RuntimeIntegrationIndependentAudit(unittest.TestCase):
 def test_resume_prior_failure_new_success_counts(self):
  class FirstError(Stub):
   def request(self,route,payload=None):
    self.fail=route=='/api/generate' and self.generation_requests==0
    return super().request(route,payload)
  d,r,c=inputs()
  with tempfile.TemporaryDirectory() as td:
   a=pathlib.Path(td)/'a';b=pathlib.Path(td)/'b'
   m1=runner.run(d,r,c,'synthetic_spec',a,True,2,transport_factory=FirstError,renderer_factory=Renderer)
   self.assertEqual(m1['recorded_failures'],1)
   m2=runner.run(d,r,c,'synthetic_spec',b,True,resume=a,transport_factory=Stub,renderer_factory=Renderer)
   v=monitor.read_snapshot(b)
   self.assertEqual((v['status'],v['failed'],v['reused_records'],v['new_attempts']),('completed',0,2,22))
   self.assertEqual(m2['recorded_failures'],1);self.assertEqual(m2['cumulative_generation_request_attempts'],24)
 def test_telemetry_finalizing_failure_does_not_skip_manifest(self):
  d,r,c=inputs();original=progress.Progress.set_stage
  def fail_on_finalizing(self,stage):
   if stage=='finalizing':raise OSError('PRIVATE_TELEMETRY_SENTINEL')
   return original(self,stage)
  with tempfile.TemporaryDirectory() as td:
   out=pathlib.Path(td)/'run'
   with patch.object(progress.Progress,'set_stage',fail_on_finalizing):
    try:runner.run(d,r,c,'synthetic_spec',out,True,2,transport_factory=Stub,renderer_factory=Renderer)
    except OSError:pass
   self.assertTrue((out/'run_manifest.json').exists())
   m=json.loads((out/'run_manifest.json').read_text());self.assertEqual(m['new_generation_request_attempts'],2)
   self.assertNotIn('PRIVATE_TELEMETRY_SENTINEL',(out/'run_manifest.json').read_text())

if __name__=='__main__':unittest.main()
