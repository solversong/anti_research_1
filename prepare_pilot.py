#!/usr/bin/env python3
"""Freeze fictional challenge24 and source calibration2; no model/server calls."""
import argparse
from copy import deepcopy
from pathlib import Path
import socket
import pilot_support as s
from draft_gate import atom,validate_rules
from local_runtime import require,strict_json,PinnedTokenizer
from run_draft_pilot import PROMPT_VERSION,prompt_messages,validate_data

SUSCEPTIBILITY_COLUMNS={'meropenem':'AMR_Meropenem','ertapenem':'AMR_Ertapenem','cefepime':'AMR_Cefepime',
                       'ceftriaxone':'AMR_Ceftriaxone','ciprofloxacin':'AMR_Ciprofloxacin',
                       'trimethoprim-sulfamethoxazole':'AMR_Cotrimoxa','piperacillin-tazobactam':'AMR_Piperacillin.Tazobactam'}

def data_document(mode,cases):
    return {'status':'pre_review_draft_pilot_frozen','mode':mode,'clinical_gold':False,'formal_evaluation_ready':False,
            'case_derivation':'independent of evaluated model outputs','cases':cases}

def materialize(case,values,attrs,option):
    case['facts']={};case['evidence']=[]
    for field,value in values.items():
        eid=f'E{len(case["evidence"])+1:03d}'
        case['facts'][field]=atom(value,[eid],source='synthetic_spec_assumption')
        case['evidence'].append({'evidence_id':eid,'field':field,'value':value,'availability_status':'fictional_time_assumption',
            'available_at':None,'source_type':'synthetic_spec_assumption'})
    option=deepcopy(option);option['source_supported_attributes']={}
    for field,value in attrs.items():
        eid=f'E{len(case["evidence"])+1:03d}'
        option['source_supported_attributes'][field]=atom(value,[eid],source='synthetic_spec_assumption')
        case['evidence'].append({'evidence_id':eid,'field':field,'value':value,'availability_status':'fictional_time_assumption',
            'available_at':None,'source_type':'synthetic_spec_assumption'})
    case['options']=[option]
    return case

def synthetic_cases(rules):
    fields=rules['field_contract']
    base={k:False for k,v in fields.items() if not k.startswith('candidate.') and v['value_type']=='boolean'}
    for k in ('case.adult','case.enterobacterales_bsi','case.monomicrobial','temporal.T_established','temporal.AST_available_by_T',
              'baseline.current_regimen_established','evidence.core_consistent','patient.hemodynamically_stable','patient.oral_absorption_adequate',
              'infection.source_control_adequate','course.clinically_improving','course.nonsevere','review.oral_transition_scope_approved',
              'organism.esbl_concern_assessed'):
        base[k]=True
    attrs={k:True for k in fields if k.startswith('candidate.') and fields[k]['value_type']=='boolean'
           and k not in ('candidate.is_oral','candidate.is_cefepime_monotherapy')}
    attrs['candidate.primary_agent_resistant']=False;attrs['candidate.contraindication_or_interaction_present']=False
    # Five manually declared rule-specification families. They are not clinical cases,
    # not independent sampled patients, and not an independent clinical gold standard.
    families=[
      ('esbl_ertapenem',{'organism.esbl':True},'ertapenem','IV','de_escalate',('candidate.primary_agent_susceptible',None),('patient.hypoalbuminemia',True),['P1'],['DEFER']),
      ('esbl_oral',{'organism.esbl':True},'ciprofloxacin','oral','de_escalate',('patient.oral_absorption_adequate',None),('review.oral_transition_scope_approved',False),['P1'],['DEFER']),
      ('ampc_cefepime',{'organism.ampc_moderate_risk':True},'cefepime','IV','de_escalate',('candidate.dosing_feasible',None),('organism.esbl',True),['P1'],['DEFER']),
      ('ampc_ceftriaxone_continuation',{'organism.ampc_moderate_risk':True,'course.ceftriaxone_started_empirically':True,'course.ceftriaxone_continuation_only':True},
       'ceftriaxone','IV','continue',('course.nonsevere',None),('course.ceftriaxone_started_empirically',False),['P1'],['DEFER']),
      ('cre_mechanism_review',{'organism.ampc_moderate_risk':True,'organism.carbapenem_resistant':True,'review.CRE_mechanism_specific_reviewed':False},
       'cefepime','IV','continue',('review.CRE_mechanism_specific_reviewed',None),('review.CRE_mechanism_specific_reviewed',True),['DEFER'],['P1'])]
    result=[]
    for family_index,(family,changes,drug,route,action,missing,exception,supported_label,exception_label) in enumerate(families,1):
        for variant in ('base','critical_unknown','explicit_condition_change','irrelevant_change'):
            values=deepcopy(base);values.update(changes);attributes=deepcopy(attrs)
            values['patient.nonclinical_nuisance_marker']=variant=='irrelevant_change'
            if variant in ('critical_unknown','explicit_condition_change'):
                key,value=missing if variant=='critical_unknown' else exception
                (attributes if key.startswith('candidate.') else values)[key]=value
            allowed=['DEFER'] if variant=='critical_unknown' else exception_label if variant=='explicit_condition_change' else supported_label
            case={'case_id':f'SYN-F{family_index}-{variant}','family_id':family,'variant':variant,'analysis_group':'in_scope_synthetic',
                'scenario_mode':'fully_fictional_rule_specification','timing_notice':'All facts, including review/time check flags, are fictional assumptions; no real clinician review occurred.',
                'derived_from_evaluation_outputs':False,'spec_allowed_options':allowed,
                'spec_label_provenance':'rule_derived_software_spec_not_clinical_gold'}
            result.append(materialize(case,values,attributes,{'option_id':'P1','action':action,'regimen':[drug],'route':route,'conditional_only':True}))
    for index,(reason,field) in enumerate([('minor','case.adult'),('non_enterobacterales','case.enterobacterales_bsi'),('non_bsi','case.enterobacterales_bsi'),('polymicrobial','case.monomicrobial')],1):
        values=deepcopy(base);values['organism.esbl']=True;values[field]=False;values['patient.nonclinical_nuisance_marker']=False
        case={'case_id':f'SYN-BOUNDARY-{index}','family_id':'scope_boundary','variant':reason,'analysis_group':'scope_boundary',
              'scenario_mode':'fully_fictional_outside_experiment_scope_boundary','timing_notice':'Outside this experimental target population; this is not a universal clinical contraindication.',
              'derived_from_evaluation_outputs':False,'spec_allowed_options':['DEFER'],'spec_label_provenance':'rule_derived_software_spec_not_clinical_gold'}
        result.append(materialize(case,values,attrs,{'option_id':'P1','action':'de_escalate','regimen':['ertapenem'],'route':'IV','conditional_only':True}))
    document=data_document('synthetic_spec',result)
    document.update({'specification_definition':'Within this closed option task P1 is required only for the declared fully supported specification; DEFER matches designated uncertainty/unsupported states. This does not establish clinical incorrectness of abstention.',
        'design':'5 arbitrary in-scope prototypes x4 variants +4 scope-boundaries','not_independent_patients':True,
        'labels_declared_before_model_outputs':True,'development_test_fixture_overlap':'challenge rows not imported by unit tests; shared rule logic and domains acknowledged'})
    return document

def actual_calibration(pack,rules):
    require(pack.get('schema_version')=='clinical_review_candidates.v1','PREPARED_SOURCE_SCHEMA_REQUIRED')
    expected=s.digest({k:v for k,v in pack.items() if k!='content_sha256_excluding_this_field'})
    require(pack.get('content_sha256_excluding_this_field')==expected,'PREPARED_SOURCE_HASH_MISMATCH')
    selected=[c for c in pack['cases'] if c['proposed_review_partition']=='calibration']
    protected=[c for c in pack['cases'] if c['proposed_review_partition'] in ('fixed_candidate','reserve')]
    require(len(selected)==2 and len(protected)==10,'CALIBRATION_TWO_PROTECTED_TEN_REQUIRED')
    result=[]
    for source in selected:
        require(source['model_payload_ready'] is False and source['clinical_gold_ready'] is False and source['timepoint']['T'] is None
                and source['current_antibiotic']['regimen'] is None,'UNRESOLVED_SOURCE_CONTRACT_REQUIRED')
        evidence=[];byfield={}
        for old in source['source_evidence']:
            require(old['field'] in ('adult','age_band','microorganism','infection_type','class','request_drug_name_mentions') or old['field'].startswith('AMR_'),'NONSTRUCTURED_SOURCE_FIELD_FORBIDDEN')
            if old['field']=='class':continue  # Classification provenance is not established as an input fact.
            eid=f'E{len(evidence)+1:03d}'
            e={'evidence_id':eid,'field':old['field'],'value':old['value'],'availability_status':'unverified',
               'available_at':None,'source_type':'recorded_structured_untimed'}
            evidence.append(e);byfield[old['field']]=e
        facts={k:atom(None,source='unknown') for k in rules['field_contract'] if not k.startswith('candidate.')}
        # No taxonomic/BSI/current-therapy eligibility is silently inferred from an
        # episode-level merged table. Recorded values remain available as evidence.
        if source['eligibility'].get('adult') is True and 'adult' in byfield:
            facts['case.adult']=atom(True,[byfield['adult']['evidence_id']],source='derived_from_recorded_structure_unverified')
        # AMR_ESBL +/- remains only a recorded category. Unverified dictionary,
        # isolate linkage and availability cannot become a confirmed Boolean.
        options=[]
        for index,drug in enumerate(SUSCEPTIBILITY_COLUMNS,1):
            attributes={k:atom(None,source='unknown') for k in rules['field_contract'] if k.startswith('candidate.') and k not in ('candidate.action','candidate.regimen','candidate.is_oral','candidate.is_cefepime_monotherapy')}
            # S/I/R is shown verbatim only. Even recorded S is NOT upgraded to
            # interpreted susceptible=true until AST dictionary/exposure is verified.
            options.append({'option_id':f'P{index}','action':'de_escalate','regimen':[drug],
                            'route':'oral' if drug in ('ciprofloxacin','trimethoprim-sulfamethoxazole') else 'IV',
                            'conditional_only':True,'source_supported_attributes':attributes})
        result.append({'case_id':source['case_id'],'origin_partition':'calibration','analysis_group':'actual_development_exposed',
            'scenario_mode':'retrospective_untimed_review','facts':facts,'evidence':evidence,'options':options,
            'timing_notice':'T, AST availability, baseline/current regimen, interpretation standard and clinical context are unverified. Candidate drug/route is a hypothetical review option, not established current use or clinical advice.',
            'source_case_sha256':s.digest(source),'derived_from_evaluation_outputs':False,'formal_model_payload_ready':False,
            'pilot_exposure':'calibration_development_only; not reusable as untouched final holdout'})
    doc=data_document('retrospective_untimed_review',result)
    doc.update({'source_pack_content_sha256':expected,'protected_case_count':10,'protected_cases_in_model_payload':0,
                'source_flags_modified':False,'source_raw_identifiers_or_text_read':False,'current_regimen_inferred_from_mentions':False})
    return doc

def main():
    p=argparse.ArgumentParser(description=__doc__)
    for key in ('rules','actual-candidates','tokenizer','output'):p.add_argument('--'+key,type=Path,required=True)
    p.add_argument('--num-ctx',type=int,default=32768)
    args=p.parse_args();require(socket.gethostname()=='Ericui-MacStudio.local','WRONG_HOST');require(not args.output.exists(),'OUTPUT_EXISTS')
    rules=strict_json(args.rules.read_text());rule_hash=validate_rules(rules)
    synthetic=synthetic_cases(rules);actual=actual_calibration(strict_json(args.actual_candidates.read_text()),rules)
    validate_data(synthetic,'synthetic_spec');validate_data(actual,'retrospective_untimed_review')
    allowed=PinnedTokenizer.FILES
    hashes={f.name:s.file_sha(f) for f in args.tokenizer.iterdir() if f.is_file() and f.name in allowed}
    config={'model_name':'qwen3.5:27b','model_digest':'7653528ba5cba4dd8e19da24aaddc7f4d0b5ecd93571c0825dfd4137958ec06e',
            'base_url':'http://127.0.0.1:11434','seed':20261005,'temperature':0,'num_ctx':args.num_ctx,'max_output_tokens':1200,
            'timeout_seconds':300,'max_consecutive_failures':3,'keep_alive_seconds':300,'think':False,'retries':0,
            'context_safety_margin_tokens':256,'tokenizer_count_tolerance':2,'server_cloud_disabled_confirmed':True,
            'tokenizer_path':str(args.tokenizer.resolve()),'tokenizer_files_sha256':hashes,'prompt_version':PROMPT_VERSION,
            'allow_unreviewed_local_draft_pilot':True,'external_api_allowed':False,'provisional_model_selection_not_final_thesis_choice':True}
    renderer=PinnedTokenizer(config);counts=[]
    for doc in (synthetic,actual):
        for case in doc['cases']:
            n=renderer.render(prompt_messages(case,rules,doc['mode']))['prompt_tokens'];counts.append(n)
            require(n+1200+256<=args.num_ctx,'WHOLE_SET_CONTEXT_OVERFLOW_NO_TRUNCATION')
    args.output.mkdir(parents=True,mode=0o700)
    for name,value in [('synthetic24.json',synthetic),('actual_calibration2.json',actual),('config.json',config),('rules_frozen.json',rules)]:s.private_write(args.output/name,value)
    s.private_write(args.output/'freeze_manifest.json',{'version':'draft_pilot_inputs_v0.1','rules_sha256':rule_hash,
        'frozen_before_model_calls':True,'synthetic_cases':24,'synthetic_in_scope':20,'scope_boundary':4,'actual_cases':2,'protected_unexposed':10,
        'clinical_gold':False,'prompt_tokens_min':min(counts),'prompt_tokens_max':max(counts),'model_calls':0,'network_requests':0,
        'synthetic_unique_public_payloads':len({s.digest({k:c[k] for k in ('scenario_mode','facts','evidence','options','timing_notice')}) for c in synthetic['cases']}),
        'synthetic_boundary_nonBSI_nonEnterobacterales_public_duplicate_preserved':True,
        'files_sha256':{f.name:s.file_sha(f) for f in args.output.iterdir() if f.is_file()},
        'generator_code_sha256':s.file_sha(Path(__file__))})
    print(s.canonical({'prepared_synthetic':24,'prepared_actual_calibration':2,'protected_unexposed':10,'prompt_tokens_min':min(counts),'prompt_tokens_max':max(counts),'model_calls':0,'network_requests':0}))

if __name__=='__main__':
    try:main()
    except s.ContractError as e:raise SystemExit(str(e)) from None
