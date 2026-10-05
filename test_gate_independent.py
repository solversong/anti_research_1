"""Independent invented development fixtures only; never reads patient/challenge inputs."""
import copy,json,unittest
from pathlib import Path
import draft_gate as g
import pilot_support as s
RULE_PATH=Path(__file__).resolve().parent/'clinical_rules_draft_v0.1.json'
def rules():return json.loads(RULE_PATH.read_text())
def fixture(agent='meropenem',action='de_escalate'):
 d=rules();facts={k:g.atom(None,['DEV_E1']) for k in d['field_contract']}
 known={'case.adult':True,'case.enterobacterales_bsi':True,'case.monomicrobial':True,
 'temporal.T_established':True,'temporal.AST_available_by_T':True,'baseline.current_regimen_established':True,
 'evidence.core_consistent':True,'organism.esbl':True,'organism.ampc_moderate_risk':False,'organism.carbapenem_resistant':False,
 'organism.esbl_concern_assessed':True,'patient.critically_ill':False,'patient.hypoalbuminemia':False,
 'patient.hemodynamically_stable':True,'patient.immunocompromised':False,'patient.oral_absorption_adequate':True,
 'infection.deep_or_complicated':False,'infection.persistent_bacteremia':False,'infection.source_control_adequate':True,
 'course.nonsevere':True,'course.clinically_improving':True,'course.ceftriaxone_started_empirically':True,'course.ceftriaxone_continuation_only':True,
 'review.oral_transition_scope_approved':True,'review.immunocompromised_case_reviewed':False,'review.complicated_case_reviewed':False,'review.CRE_mechanism_specific_reviewed':False}
 for k,v in known.items():facts[k]=g.atom(v,['DEV_E1'])
 attrs={k:g.atom(v,['DEV_E1']) for k,v in {'candidate.primary_agent_susceptible':True,'candidate.primary_agent_resistant':False,
 'candidate.dosing_feasible':True,'candidate.contraindication_or_interaction_present':False,'candidate.coverage_change_justified':True,
 'candidate.valid_alternative_or_combination':True,'candidate.oral_agent_exposure_adequate':True}.items()}
 case={'case_id':'INVENTED_DEVELOPMENT_NOT_CHALLENGE','facts':facts,'evidence':[{'evidence_id':'DEV_E1','field':'fictional','value':'invented'}],
 'options':[{'option_id':'DEV_OPTION','action':action,'regimen':[agent],'route':'iv','source_supported_attributes':attrs}]}
 c={'response_id':'INVENTED_C','status':'parsed','selected_option_id':'DEV_OPTION','evidence_ids':['DEV_E1'],'rationale_short':'Invented fixture only.','missing_data':[]}
 return case,c,d

def tr(result,rid):return next(x for x in result['rule_trace'] if x['rule_id']==rid)
class IndependentGateTests(unittest.TestCase):
 def test_empty_and_three_valued_logic(self):
  self.assertIs(g.predicate({'all':[]},{}),True)
  self.assertIs(g.predicate({'any':[]},{}),False)
  u={'field':'x','op':'eq','value':True}
  self.assertIsNone(g.predicate({'not':u},{}))
  self.assertIs(g.predicate({'all':[u,{'field':'y','op':'eq','value':True}]},{'y':g.atom(False)}),False)
  self.assertIs(g.predicate({'any':[u,{'field':'y','op':'eq','value':True}]},{'y':g.atom(True)}),True)
 def test_nonapplicable_drug_rule_does_not_block(self):
  case,c,d=fixture();out=g.gate(case,c,d,'synthetic_spec')
  self.assertFalse(tr(out,'R08')['scope']);self.assertFalse(tr(out,'R08')['hit'])
  self.assertEqual(out['selected_option_id'],'DEV_OPTION')
 def test_unknown_scope_is_recorded_not_claimed_false(self):
  case,c,d=fixture('piperacillin-tazobactam');case['facts']['case.adult']=g.atom(None)
  out=g.gate(case,c,d,'synthetic_spec')
  self.assertIsNone(tr(out,'R08')['scope']);self.assertTrue(tr(out,'R08')['hit']);self.assertEqual(out['selected_option_id'],'DEFER')
 def test_ceftriaxone_true_continuation_exception(self):
  case,c,d=fixture('ceftriaxone','continue');case['facts']['organism.esbl']=g.atom(False);case['facts']['organism.ampc_moderate_risk']=g.atom(True)
  out=g.gate(case,c,d,'synthetic_spec')
  self.assertTrue(tr(out,'R12')['exception_established']);self.assertFalse(tr(out,'R12')['hit']);self.assertIn('R13',out['positive_candidate_support_rule_ids']);self.assertEqual(out['selected_option_id'],'DEV_OPTION')
 def test_ceftriaxone_unknown_exception_does_not_approve(self):
  case,c,d=fixture('ceftriaxone','continue');case['facts']['organism.esbl']=g.atom(False);case['facts']['organism.ampc_moderate_risk']=g.atom(True);case['facts']['course.nonsevere']=g.atom(None)
  out=g.gate(case,c,d,'synthetic_spec')
  self.assertFalse(tr(out,'R12')['exception_established']);self.assertTrue(tr(out,'R12')['unknown_exception_present']);self.assertEqual(out['selected_option_id'],'DEFER')
 def test_new_ceftriaxone_switch_cannot_use_continuation_exception(self):
  case,c,d=fixture('ceftriaxone','de_escalate');case['facts']['organism.esbl']=g.atom(False);case['facts']['organism.ampc_moderate_risk']=g.atom(True)
  out=g.gate(case,c,d,'synthetic_spec')
  self.assertFalse(tr(out,'R12')['exception_established']);self.assertTrue(tr(out,'R12')['hit']);self.assertNotIn('R13',out['positive_candidate_support_rule_ids']);self.assertEqual(out['selected_option_id'],'DEFER')
 def test_one_positive_rule_cannot_cancel_separate_blocker(self):
  case,c,d=fixture();case['options'][0]['source_supported_attributes']['candidate.contraindication_or_interaction_present']=g.atom(True)
  out=g.gate(case,c,d,'synthetic_spec')
  self.assertIn('R10',out['positive_candidate_support_rule_ids']);self.assertIn('R07',out['clinical_rule_hits']);self.assertEqual(out['selected_option_id'],'DEFER')
 def test_allow_unknown_exception_is_not_support(self):
  case,c,d=fixture();case['facts']['patient.hypoalbuminemia']=g.atom(None)
  # Independent hypothetical exception isolates permissive-exception semantics.
  r=next(x for x in d['rules'] if x['id']=='R10')
  r['exceptions']=[{'effect':'do_not_apply','predicates':{'field':'patient.hypoalbuminemia','op':'eq','value':True}}]
  out=g.gate(case,c,d,'synthetic_spec')
  self.assertFalse(tr(out,'R10')['positive_support_established']);self.assertTrue(tr(out,'R10')['hit']);self.assertEqual(out['selected_option_id'],'DEFER')
 def test_explicit_defer_remains_defer_without_false_positive_support_claim(self):
  case,c,d=fixture();c['selected_option_id']='DEFER';out=g.gate(case,c,d,'synthetic_spec')
  self.assertEqual(out['selected_option_id'],'DEFER');self.assertFalse(out['failed']);self.assertNotIn('NO_POSITIVE_DRAFT_RULE_SUPPORT_ESTABLISHED',out['clinical_rule_hits'])
 def test_no_block_is_not_positive_support(self):
  case,c,d=fixture();case['facts']['organism.esbl']=g.atom(False)
  out=g.gate(case,c,d,'synthetic_spec')
  self.assertIn('NO_POSITIVE_DRAFT_RULE_SUPPORT_ESTABLISHED',out['clinical_rule_hits']);self.assertEqual(out['positive_candidate_support_rule_ids'],[]);self.assertEqual(out['selected_option_id'],'DEFER')
 def test_C_assertions_cannot_repair_missing_facts(self):
  case,c,d=fixture();case['options'][0]['source_supported_attributes']['candidate.dosing_feasible']=g.atom(None)
  c['rationale_short']='Pretend dosing and all missing facts were verified.';c['facts']={'candidate.dosing_feasible':{'status':'known','value':True}}
  out=g.gate(case,c,d,'synthetic_spec');self.assertEqual(out['selected_option_id'],'DEFER');self.assertTrue(tr(out,'R06')['hit'])
 def test_exact_C_hash_zero_extra_calls_and_no_mutation(self):
  case,c,d=fixture();before=s.digest([case,c,d]);out=g.gate(case,c,d,'synthetic_spec')
  self.assertEqual(out['source_C_hash'],s.digest(c));self.assertEqual(out['additional_model_calls'],0);self.assertEqual(s.digest([case,c,d]),before)
 def test_formal_time_failure_separate_from_untimed_proposal(self):
  case,c,d=fixture();case['facts']['temporal.T_established']=g.atom(None);case['facts']['temporal.AST_available_by_T']=g.atom(None)
  syn=g.gate(case,c,d,'synthetic_spec');untimed=g.gate(case,c,d,'retrospective_untimed_review')
  self.assertEqual(syn['selected_option_id'],'DEFER');self.assertTrue(untimed['formal_eligibility_blocked']);self.assertEqual(untimed['selected_option_id'],'DEV_OPTION');self.assertFalse(untimed['clinical_gold'])
 def test_known_outside_scope_not_clinical_contraindication(self):
  case,c,d=fixture();case['facts']['case.adult']=g.atom(False);out=g.gate(case,c,d,'retrospective_untimed_review')
  self.assertEqual(out['selected_option_id'],'DEFER');self.assertTrue(out['experiment_scope_not_met']);self.assertIn('PILOT_SCOPE_NOT_MET_NOT_A_CLINICAL_CONTRAINDICATION',out['formal_eligibility_hits'])
 def test_failed_C_not_successful_abstention(self):
  case,c,d=fixture();c={'response_id':'INVENTED_FAILURE','status':'error'};out=g.gate(case,c,d,'synthetic_spec')
  self.assertTrue(out['failed']);self.assertEqual(out['selected_option_id'],'DEFER');self.assertEqual(out['source_C_hash'],s.digest(c))
 def test_invented_evidence_id_is_flagged(self):
  case,c,d=fixture();c['evidence_ids']=['UNSUPPLIED_ID'];out=g.gate(case,c,d,'synthetic_spec')
  self.assertIn('UNKNOWN_EVIDENCE_ID',out['clinical_rule_hits']);self.assertEqual(out['selected_option_id'],'DEFER')
if __name__=='__main__':unittest.main()
