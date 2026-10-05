"""Invented development fixtures / in-process transport only; no challenge rows."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
import run_draft_pilot as r
import draft_gate as g
import pilot_support as s
from test_gate_independent import fixture,rules

def inputs():
    seed,_,rule_doc=fixture()
    case={'case_id':'DEV_RUN','scenario_mode':'invented_development_fixture','facts':{},'evidence':[{'evidence_id':'E001','field':'evidence.core_consistent',
          'value':True,'availability_status':'fictional_time_assumption','available_at':None,'source_type':'synthetic_spec_fixture'}],
          'options':copy.deepcopy(seed['options']),'timing_notice':'Invented development fixture, not a clinical case.',
          'derived_from_evaluation_outputs':False,'spec_allowed_options':['P1'],'spec_label_provenance':'rule_derived_software_spec_not_clinical_gold'}
    for key,value in seed['facts'].items():
        if not key.startswith('candidate.'):case['facts'][key]=g.atom(value['value'],['E001'])
    option=case['options'][0];option.update({'option_id':'P1','route':'IV','conditional_only':True})
    for value in option['source_supported_attributes'].values():value['evidence_ids']=['E001']
    cases=[]
    for i in range(24):
        c=copy.deepcopy(case);c['case_id']=f'DEV_RUN_{i}';c['analysis_group']='in_scope_synthetic' if i<20 else 'scope_boundary'
        if i>=20:c['facts']['case.adult']=g.atom(False,['E001']);c['spec_allowed_options']=['DEFER']
        cases.append(c)
    data={'status':'pre_review_draft_pilot_frozen','mode':'synthetic_spec','clinical_gold':False,'formal_evaluation_ready':False,'cases':cases}
    config={'model_name':'qwen3.5:27b','model_digest':'7653528ba5cba4dd8e19da24aaddc7f4d0b5ecd93571c0825dfd4137958ec06e',
        'base_url':'http://127.0.0.1:11434','seed':1,'temperature':0,'num_ctx':4096,'max_output_tokens':100,'context_safety_margin_tokens':256,
        'tokenizer_count_tolerance':2,'max_consecutive_failures':3,'keep_alive_seconds':0,'timeout_seconds':5,'think':False,'retries':0,
        'server_cloud_disabled_confirmed':True,'allow_unreviewed_local_draft_pilot':True,'external_api_allowed':False,'prompt_version':r.PROMPT_VERSION}
    return data,rule_doc,config

class Renderer:
    def __init__(self,config):pass
    def render(self,messages):return {'prompt':s.canonical(messages),'prompt_tokens':100}

class Stub:
    def __init__(self,config,fail=False):self.config=config;self.fail=fail;self.network_requests=0;self.generation_requests=0
    def request(self,route,payload=None):
        self.network_requests+=1
        if route=='/api/tags':value={'models':[{'name':self.config['model_name'],'digest':self.config['model_digest'],'size':123,'details':{'format':'gguf'}}]}
        else:
            self.generation_requests+=1
            answer={'selected_option_id':'P1','rationale_short':'Invented stub explanation.','evidence_ids':['E001'],'missing_data':[]}
            value={'model':self.config['model_name'],'done':True,'done_reason':'stop','prompt_eval_count':100,'eval_count':30,'response':'broken' if self.fail else s.canonical(answer)}
        return {'http_status':200,'raw_body':s.canonical(value),'elapsed_seconds':0.001}

class RunnerTests(unittest.TestCase):
    def test_first_two_then_remaining_same_frozen_contract(self):
        data,rule_doc,config=inputs()
        with tempfile.TemporaryDirectory() as folder:
            first=Path(folder)/'first';last=Path(folder)/'last'
            m=r.run(data,rule_doc,config,'synthetic_spec',first,True,2,transport_factory=Stub,renderer_factory=Renderer)
            self.assertEqual(m['new_generation_request_attempts'],2)
            self.assertEqual(m['status'],'PLANNED_PREFIX_COMPLETE_CONTINUATION_PENDING')
            n=r.run(data,rule_doc,config,'synthetic_spec',last,True,resume=first,transport_factory=Stub,renderer_factory=Renderer)
            self.assertEqual(n['new_generation_request_attempts'],22);self.assertEqual(n['cumulative_generation_request_attempts'],24)
            cs=json.loads((last/'C_responses.json').read_text());ds=json.loads((last/'D_results.json').read_text())
            self.assertEqual(len(cs),24);self.assertTrue(all(d['source_C_hash']==s.digest(c) for c,d in zip(cs,ds)))
            self.assertEqual((last/'calls/0003_C.json').stat().st_mode&0o777,0o600)

    def test_changed_config_cannot_resume(self):
        data,rule_doc,config=inputs()
        with tempfile.TemporaryDirectory() as folder:
            first=Path(folder)/'first';r.run(data,rule_doc,config,'synthetic_spec',first,True,2,transport_factory=Stub,renderer_factory=Renderer)
            config['seed']=2
            with self.assertRaisesRegex(s.ContractError,'RESUME_FROZEN_CONTRACT_MISMATCH'):
                r.run(data,rule_doc,config,'synthetic_spec',Path(folder)/'next',True,resume=first,transport_factory=Stub,renderer_factory=Renderer)

    def test_three_failures_no_retry_full_denominator(self):
        data,rule_doc,config=inputs()
        with tempfile.TemporaryDirectory() as folder:
            out=Path(folder)/'run';m=r.run(data,rule_doc,config,'synthetic_spec',out,True,transport_factory=lambda c:Stub(c,True),renderer_factory=Renderer)
            self.assertEqual(m['new_generation_request_attempts'],3);self.assertEqual(m['recorded_failures'],3)
            summary=json.loads((out/'draft_pilot_summary.json').read_text())
            self.assertEqual(summary['planned_cases'],24);self.assertEqual(summary['unattempted_cases'],21)
            self.assertEqual(summary['D_non_defer'],0)

    def test_no_network_in_dry_run_or_overflow(self):
        data,rule_doc,config=inputs()
        bomb=lambda _:self.fail('Transport constructed')
        with tempfile.TemporaryDirectory() as folder:
            m=r.run(data,rule_doc,config,'synthetic_spec',Path(folder)/'dry',transport_factory=bomb,renderer_factory=Renderer)
            self.assertEqual(m['network_requests'],0)
            class Long(Renderer):
                def render(self,m):return {'prompt':'fixture','prompt_tokens':5000}
            with self.assertRaisesRegex(s.ContractError,'WHOLE_SET_CONTEXT_OVERFLOW'):
                r.run(data,rule_doc,config,'synthetic_spec',Path(folder)/'long',True,transport_factory=bomb,renderer_factory=Long)

    def test_no_gold_or_reference_in_public_prompt(self):
        data,rule_doc,config=inputs();case=data['cases'][0]
        case['spec_allowed_options']=['SECRET_SPEC_LABEL']
        text=s.canonical(r.prompt_messages(case,rule_doc,'synthetic_spec'))
        self.assertNotIn('SECRET_SPEC_LABEL',text);self.assertNotIn('spec_allowed_options',text)

    def test_reject_nested_reference_and_gold_source(self):
        for change in ('field','source','nested','status'):
            data,_,_=inputs();c=data['cases'][0]
            if change=='field':c['facts']['gold.allowed_decision']=g.atom(True)
            elif change=='source':c['facts']['case.adult']['source_type']='clinician_gold'
            elif change=='nested':c['evidence'][0]['value']={'gold':'SECRET'}
            else:c['facts']['case.adult']['status']='unknown'
            with self.assertRaises(s.ContractError):r.validate_data(data,'synthetic_spec')

    def test_actual_may_not_include_protected_partition_or_labels(self):
        data,_,_=inputs();data['mode']='retrospective_untimed_review';data['cases']=data['cases'][:2];data['protected_case_count']=10
        for c in data['cases']:
            c['origin_partition']='fixed_candidate';c['formal_model_payload_ready']=False
        with self.assertRaisesRegex(s.ContractError,'ONLY_ORIGINAL_CALIBRATION_TWO_ALLOWED'):
            r.validate_data(data,'retrospective_untimed_review')

    def test_baseline_and_false_hold_denominators_present(self):
        data,_,_=inputs();summary=r.summarize(data['cases'],[],[],'synthetic_spec')
        group=summary['software_spec_counts_by_group']['in_scope_synthetic']
        self.assertEqual(group['denominator'],20);self.assertEqual(group['always_DEFER_spec_matches'],0)
        self.assertEqual(group['D_DEFER_outside_spec_allowed_set'],0)
        actual=r.summarize(data['cases'][:2],[],[],'retrospective_untimed_review')
        self.assertEqual(actual['planned_actual_cases'],2);self.assertEqual(actual['actual_cases_exposed'],0)

if __name__=='__main__':unittest.main()
