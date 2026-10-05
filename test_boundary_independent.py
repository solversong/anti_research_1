"""Independent privacy/lineage boundary tests with invented inputs only."""
import copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import prepare_pilot as p
import run_draft_pilot as r
import pilot_support as s
from test_runner import inputs,Renderer,Stub
from test_gate_independent import rules

def invented_pack():
 cases=[]
 for i in range(12):
  cases.append({'case_id':'FICTIONAL_CASE_'+str(i),'proposed_review_partition':'calibration' if i<2 else 'fixed_candidate' if i<8 else 'reserve',
   'model_payload_ready':False,'clinical_gold_ready':False,'timepoint':{'T':None},'current_antibiotic':{'regimen':None},'eligibility':{'adult':True},
   'source_evidence':[{'field':'adult','value':True},{'field':'AMR_ESBL','value':'+'},{'field':'class','value':'INVENTED_CLASS_SENTINEL'}],
   'researcher_draft':{'allowed_decision':'SECRET_DRAFT_SENTINEL'}})
 pack={'schema_version':'clinical_review_candidates.v1','cases':cases}
 pack['content_sha256_excluding_this_field']=s.digest(pack)
 return pack
class IndependentBoundaryTests(unittest.TestCase):
 def test_unattempted_actual_cases_not_counted_exposed(self):
  summary=r.summarize([{'case_id':'FICTIONAL_A'},{'case_id':'FICTIONAL_B'}],[],[],'retrospective_untimed_review')
  self.assertEqual(summary['planned_actual_cases'],2);self.assertEqual(summary['actual_cases_exposed'],0)
 def test_adapter_limits_calibration_excludes_class_and_draft(self):
  d=p.actual_calibration(invented_pack(),rules());r.validate_data(d,'retrospective_untimed_review')
  self.assertEqual(len(d['cases']),2);self.assertEqual(d['protected_case_count'],10)
  for c in d['cases']:
   self.assertIsNone(c['facts']['organism.esbl']['value']);self.assertEqual(c['facts']['organism.esbl']['status'],'unknown')
   self.assertTrue(any(e['field']=='AMR_ESBL' and e['value']=='+' for e in c['evidence']))
   prompt=s.canonical(r.prompt_messages(c,rules(),'retrospective_untimed_review'))
   self.assertNotIn('INVENTED_CLASS_SENTINEL',prompt);self.assertNotIn('SECRET_DRAFT_SENTINEL',prompt);self.assertNotIn('FICTIONAL_CASE_',prompt)
 def test_raw_consult_field_rejected_without_exposure(self):
  pack=invented_pack();pack['cases'][0]['source_evidence'].append({'field':'consultation_response','value':'SECRET_CLINICAL_JUDGMENT'})
  pack['content_sha256_excluding_this_field']=s.digest({k:v for k,v in pack.items() if k!='content_sha256_excluding_this_field'})
  with self.assertRaisesRegex(s.ContractError,'NONSTRUCTURED_SOURCE_FIELD_FORBIDDEN'):p.actual_calibration(pack,rules())
 def test_noncalibration_rows_excluded_not_renamed_into_input(self):
  pack=invented_pack();d=p.actual_calibration(pack,rules())
  self.assertEqual({c['case_id'] for c in d['cases']},{'FICTIONAL_CASE_0','FICTIONAL_CASE_1'})
  self.assertTrue(all(c['origin_partition']=='calibration' for c in d['cases']))
 def test_resume_gate_code_change_rejected_before_transport(self):
  data,rule_doc,config=inputs();original_sha=s.file_sha
  with tempfile.TemporaryDirectory() as td:
   first=Path(td)/'first';r.run(data,rule_doc,config,'synthetic_spec',first,True,1,transport_factory=Stub,renderer_factory=Renderer)
   def changed(path):return 'f'*64 if Path(path).name=='draft_gate.py' else original_sha(path)
   with patch.object(s,'file_sha',side_effect=changed):
    with self.assertRaisesRegex(s.ContractError,'RESUME_FROZEN_CONTRACT_MISMATCH'):
     r.run(data,rule_doc,config,'synthetic_spec',Path(td)/'next',True,resume=first,transport_factory=lambda _:self.fail('NO TRANSPORT'),renderer_factory=Renderer)
 def test_gold_nested_and_narrative_injection_blocked(self):
  for mutation in ('gold_source','newfield','longtext','nested'):
   d=p.actual_calibration(invented_pack(),rules());c=d['cases'][0]
   if mutation=='gold_source':c['facts']['case.adult']['source_type']='clinician_gold'
   elif mutation=='newfield':c['facts']['reference.label']=copy.deepcopy(c['facts']['case.adult'])
   elif mutation=='longtext':c['evidence'][0]['value']='SENTINEL_'*30
   else:c['evidence'][0]['value']={'final_expert_label':'SECRET'}
   with self.assertRaises(s.ContractError):r.validate_data(d,'retrospective_untimed_review')
if __name__=='__main__':unittest.main()
