from types import SimpleNamespace
import pytest
from scanner.trigger_catalog import SymbolSeries, EvalCtx, evaluate
from scanner.conditions import ConditionCtx, check

def series(sides, vwaps=None):
    s=SymbolSeries('TEST'); minute=570
    for i,side in enumerate(sides):
        v=(vwaps or [100]*len(sides))[i]
        px=v+side
        for _ in range(5):
            s.on_bar(dict(open=px+2*side,high=px+5,low=px-5,close=px,volume=100),minute,'2026-09-18',v)
            minute+=1
    s.on_bar(dict(open=100,high=100,low=100,close=100,volume=1),minute,'2026-09-18',100)
    return s

def fire(s,up=True,session='rth'):
    return evaluate('vwap_cross_confirmed',EvalCtx(SimpleNamespace(symbol='TEST'),s,{},600,session,set()),'above' if up else 'below',{'tf':5})

@pytest.mark.parametrize('sign',[1,-1])
def test_waits_for_next_close_and_does_not_repeat(sign):
    assert fire(series([-sign]*6+[sign]),sign==1) is None
    assert fire(series([-sign]*6+[sign,sign]),sign==1).direction==('long' if sign==1 else 'short')
    assert fire(series([-sign]*6+[sign,sign,sign]),sign==1) is None

@pytest.mark.parametrize('sign',[1,-1])
@pytest.mark.parametrize('confirmation',[0,-1])
def test_failed_or_equal_confirmation_does_not_alert(sign,confirmation):
    assert fire(series([-sign]*6+[sign,sign*confirmation]),sign==1) is None

@pytest.mark.parametrize('sign',[1,-1])
def test_history_is_six_before_cross_not_before_confirmation(sign):
    cfg=dict(id='vwap_hold',op='gte',value=6,option='below' if sign==1 else 'above',params=dict(tf=5,lookback=8,margin=0))
    for sides,expected in [([-sign]*6+[sign,sign],True),([-sign]*5+[0,sign,sign],False),([-sign]*5+[sign,sign,sign],False)]:
        s=series(sides)
        ctx=ConditionCtx(state=SimpleNamespace(symbol='TEST'),series=s,direction='long' if sign==1 else 'short')
        assert check(cfg,ctx).passed==expected
    assert not check(cfg,ConditionCtx(state=SimpleNamespace(symbol='TEST'),series=series([-sign]*5+[sign,sign]),direction='long' if sign==1 else 'short')).passed

def test_uses_each_candles_vwap_and_allows_red_confirmation_for_up():
    assert fire(series([-1,1,1],[101,100,98])) is not None

@pytest.mark.parametrize('problem',['missing','gap','day','session','forming'])
def test_invalid_confirmation_context_is_rejected(problem):
    s=series([-1,1,1])
    if problem=='missing':s.candles[5][-1]['vwap']=None
    if problem=='gap':s.candles[5][-1]['key']=('2026-09-18','rth',9)
    if problem=='day':s.candles[5][-1]['key']=('2026-09-21','rth',2)
    if problem=='session':s.candles[5][-1]['key']=('2026-09-18','pre',2)
    if problem=='forming':s.completed[5]=False
    assert fire(s) is None

def test_not_premarket_and_not_enough_candles():
    assert fire(series([-1,1,1]),session='pre') is None
    assert fire(series([-1,1])) is None


@pytest.mark.parametrize('sign', [1, -1])
def test_combined_setup_opposite_history_matches_split_demo(sign):
    direction = 'long' if sign == 1 else 'short'
    combined = dict(id='vwap_hold', op='gte', value=6, option='opposite',
                    params=dict(tf=5, lookback=8, margin=0))
    explicit = dict(combined, option='below' if sign == 1 else 'above')
    for prior in [[-sign]*6, [-sign]*5+[0], [-sign]*5+[sign]]:
        s = series(prior + [sign, sign])
        ctx = ConditionCtx(state=SimpleNamespace(symbol='TEST'), series=s, direction=direction)
        assert check(combined, ctx).passed == check(explicit, ctx).passed
        assert check(combined, ctx).passed == (prior == [-sign]*6)


def test_opposite_option_survives_setup_validation():
    from scanner.custom_setups import normalize_setup
    setup = normalize_setup(dict(id='cs_test_confirmed', name='Test', direction='all',
        triggers=[dict(id='vwap_cross_confirmed', options=['above', 'below'], params={'tf': 5})],
        parameters=[dict(id='vwap_hold', op='gte', value=6, option='opposite',
                         params=dict(tf=5, lookback=8, margin=0))]))
    assert setup['parameters'][0]['option'] == 'opposite'
    assert setup['triggers'][0]['options'] == ['above', 'below']
