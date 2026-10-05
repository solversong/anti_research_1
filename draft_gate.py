"""Three-valued draft-rule evaluator. No labels or C's explanations become facts."""
from copy import deepcopy
import pilot_support as s

OPS={'eq','ne','in','not_in','gte','lte','exists','contains_any','contains_all'}

def require(ok,code):
    if not ok: raise s.ContractError(code)

def lookup(facts, field):
    item=facts.get(field)
    if not isinstance(item,dict) or item.get('status')!='known' or item.get('value') is None or item.get('value')=='unknown':return None
    return item['value']

def predicate(expr,facts):
    require(isinstance(expr,dict),'PREDICATE_OBJECT_REQUIRED')
    if 'all' in expr or 'any' in expr:
        kind='all' if 'all' in expr else 'any'
        require(set(expr)=={kind} and isinstance(expr[kind],list),'BOOLEAN_EXPRESSION_INVALID')
        values=[predicate(x,facts) for x in expr[kind]]
        if kind=='all':return False if False in values else None if None in values else True
        return True if True in values else None if None in values else False
    if 'not' in expr:
        require(set(expr)=={'not'},'NOT_EXPRESSION_INVALID')
        result=predicate(expr['not'],facts)
        return None if result is None else not result
    require(set(expr) <= {'field','op','value'} and {'field','op'} <= set(expr) and expr['op'] in OPS,'ATOMIC_PREDICATE_INVALID')
    value=lookup(facts,expr['field']);expected=expr.get('value');op=expr['op']
    if op=='exists':return value is not None
    if value is None:return None
    if op in ('eq','ne'):
        result=type(value) is type(expected) and value==expected
        return result if op=='eq' else not result
    if op in ('in','not_in'):
        require(isinstance(expected,list),'IN_VALUES_MUST_BE_LIST')
        result=any(type(value) is type(v) and value==v for v in expected)
        return result if op=='in' else not result
    if op in ('contains_any','contains_all'):
        require(isinstance(expected,list),'CONTAINS_VALUES_MUST_BE_LIST')
        if not isinstance(value,list):return None
        hits=[x in value for x in expected]
        return any(hits) if op=='contains_any' else all(hits)
    if type(value) not in (int,float) or type(expected) not in (int,float):return None
    return value>=expected if op=='gte' else value<=expected

def atom(value,ids=(),source='synthetic_spec_fixture'):
    return {'value':value,'status':'unknown' if value is None else 'known','evidence_ids':list(ids),
            'available_at':None,'source_type':source}

def validate_rules(document):
    require(document.get('status')=='researcher_ai_draft_unreviewed' and document.get('clinical_gold_ready') is False
            and document.get('clinician_reviewed') is False,'UNREVIEWED_RULE_PROVENANCE_REQUIRED')
    rules=document.get('rules');require(isinstance(rules,list) and bool(rules),'RULES_MISSING')
    ids=set()
    for rule in rules:
        require(rule['id'] not in ids,'DUPLICATE_RULE_ID');ids.add(rule['id'])
        require(rule['action'] in ('block','defer','allow_candidate'),'UNSUPPORTED_RULE_ACTION')
        require(rule['action_scope'] in ('candidate_proposal','formal_eligibility'),'UNSUPPORTED_ACTION_SCOPE')
        require(rule['unknown_policy'] in ('defer','not_applicable','record_only'),'UNKNOWN_POLICY_REQUIRED')
        predicate(rule['predicates'],{})  # Structural check; actual facts may be unknown.
        predicate(rule['scope'],{})
        for exception in rule.get('exceptions',[]):
            require(isinstance(exception,dict) and exception.get('effect')=='do_not_apply','EXECUTABLE_EXCEPTION_REQUIRED')
            predicate(exception['predicates'],{})
    return s.digest(document)

def option_context(case,selected_option_id):
    options={o['option_id']:o for o in case['options']}
    require(selected_option_id in options or selected_option_id=='DEFER','UNKNOWN_SELECTED_OPTION')
    option={'option_id':'DEFER','action':'abstain','regimen':[],'route':None} if selected_option_id=='DEFER' else options[selected_option_id]
    facts=deepcopy(case['facts'])
    # Candidate-specific clinical attributes are supplied with source provenance in
    # the case, never extracted from generated rationale/missing_data assertions.
    for name,item in option.get('source_supported_attributes',{}).items():
        require(name.startswith('candidate.'),'INVALID_CANDIDATE_ATTRIBUTE')
        facts[name]=deepcopy(item)
    facts['candidate.action']=atom(option['action'],source='fixed_option_contract')
    facts['candidate.regimen']=atom(option['regimen'],source='fixed_option_contract')
    facts['candidate.is_oral']=atom(option.get('route')=='oral' if option.get('route') is not None else None,source='fixed_option_contract')
    facts['candidate.is_cefepime_monotherapy']=atom(option['regimen']==['cefepime'],source='fixed_option_contract')
    return option,facts

def gate(case,c_response,rules,mode):
    require(mode in ('synthetic_spec','retrospective_untimed_review'),'INVALID_PILOT_MODE')
    rule_hash=validate_rules(rules)
    failed=c_response.get('status')!='parsed'
    selected=c_response.get('selected_option_id') if not failed else 'DEFER'
    option,facts=option_context(case,selected)
    clinical_hits=[];formal_hits=[];trace=[];positive_support=[]
    for rule in rules['rules']:
        scope=predicate(rule['scope'],facts)
        trigger=predicate(rule['predicates'],facts)
        result=False if scope is False else None if scope is None else trigger
        exceptions=[predicate(e['predicates'],facts) for e in rule.get('exceptions',[])]
        excepted=result is True and True in exceptions
        apply=result is True and not excepted
        uncertain=result is None and not excepted
        action=rule['action']
        effective_action='defer' if result is True and any(v is None and e.get('unknown_exception_policy')=='defer' for e,v in zip(rule.get('exceptions',[]),exceptions)) else action
        hit=(apply and action in ('block','defer')) or (uncertain and rule['unknown_policy']=='defer')
        # For a permissive rule, all safety exceptions must be established false.
        # For a restrictive rule, an unknown relaxation exception never lifts it.
        support=apply and action=='allow_candidate' and None not in exceptions
        if action=='allow_candidate' and result is True and None in exceptions:hit=True
        # Unknown exception never establishes an exception. Record it explicitly.
        item={'rule_id':rule['id'],'scope':scope,'trigger_predicate':trigger,'effective_predicate':result,'exception_results':exceptions,'exception_established':excepted,
              'action':action,'effective_action':effective_action,'action_scope':rule['action_scope'],'hit':hit,
              'hit_reason':'unknown_exception' if effective_action!=action else 'unknown_condition' if uncertain else 'known_trigger' if apply else 'not_triggered',
              'unknown_exception_present':None in exceptions,'rationale':rule['rationale']}
        item['positive_support_established']=support
        trace.append(item)
        if support and rule['action_scope']=='candidate_proposal':positive_support.append(rule['id'])
        if hit:(formal_hits if rule['action_scope']=='formal_eligibility' else clinical_hits).append(rule['id'])
    outside=any(lookup(facts,key) is False for key in ('case.adult','case.enterobacterales_bsi','case.monomicrobial'))
    if outside:formal_hits.append('PILOT_SCOPE_NOT_MET_NOT_A_CLINICAL_CONTRAINDICATION')
    knownids={e['evidence_id'] for e in case['evidence']}
    badids=any(x not in knownids for x in c_response.get('evidence_ids',[]))
    if badids:clinical_hits.append('UNKNOWN_EVIDENCE_ID')
    if selected!='DEFER' and not positive_support:clinical_hits.append('NO_POSITIVE_DRAFT_RULE_SUPPORT_ESTABLISHED')
    # Formal T/current-regimen constraints stay visible in retrospective mode, but
    # do not become a fabricated clinical defer reference or a claim of correct T.
    must_defer=failed or bool(clinical_hits) or outside or (mode=='synthetic_spec' and bool(formal_hits))
    d_selected='DEFER' if must_defer else selected
    result={'source_C_response_id':c_response['response_id'],'source_C_hash':s.digest(c_response),'rule_document_sha256':rule_hash,
        'selected_option_id':d_selected,'original_C_option_id':selected,'clinical_rule_hits':clinical_hits,
        'formal_eligibility_hits':formal_hits,'formal_eligibility_blocked':bool(formal_hits),
        'experiment_scope_not_met':outside,'rule_trace':trace,'failed':failed,'additional_model_calls':0,
        'positive_candidate_support_rule_ids':positive_support,
        'not_blocked_is_not_equivalent_to_supported':True,
        'clinical_gold':False,'mode':mode,'rules_status':'researcher_ai_draft_unreviewed'}
    result['D_sha256']=s.digest(result)
    return result
