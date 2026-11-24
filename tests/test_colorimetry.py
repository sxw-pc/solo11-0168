"""Tests for offline panel capture and color-version certification.

The first group guards the physics: the exact four-band metamer witness, the
3x3 camera matrix that wrongly signs it off, the between-filter narrow peak,
and the conditions under which a color difference can actually be proved.  The
second group guards the ledger: calibration validity, envelope coverage, the
primary/fallback route distinction, the booking color gate and revocation
freezing.
"""
import json,subprocess,sys
from pathlib import Path
import pytest

from adflight import colorimetry as cm
from adflight import colorpipe,creative,schedule
from adflight.report import report,audit

ROOT=Path(__file__).resolve().parent.parent
FIX=ROOT/'fixtures';COLOR=FIX/'color'
TOY_GRID=[1.0,2.0,3.0,4.0]


def load(name):return json.loads((COLOR/name).read_text())
def write(tmp,name,data):
    p=tmp/name;p.write_text(json.dumps(data));return p


# --------------------------------------------------------------------------
# physics: the camera is not the eye
# --------------------------------------------------------------------------
def test_toy_witness_same_camera_different_eye_color():
    w=cm.toy_witness()
    assert w['camera_A']==w['camera_B']==[1,1,1]
    assert w['eye_A']==[1,1,1] and w['eye_B']==[0,0,2]
    assert w['camera_identical'] and w['eye_different']
    # the null spectrum is invisible to the camera but visible to the eye
    assert cm.norm(cm.matvec(cm.TOY['camera'],w['null_spectrum']))<1e-12
    assert cm.norm(w['observer_sees_null'])>0
    # the shipped fixture preserves the complete numeric witness, including the
    # exact ambiguous observer box the camera-only pipeline cannot escape
    doc=load('toy-witness.json')
    assert doc['camera_identical'] and doc['eye_different']
    assert doc['camera_A']==[1,1,1] and doc['eye_B']==[0,0,2]
    assert doc['camera_certification_for_B_vs_A_target']['observer_box']==[
        [0.0,1.0],[0.0,1.0],[1.0,2.0]]
    assert doc['camera_certification_for_B_vs_A_target']['status']=='rejected'


def test_standard_3x3_camera_matrix_wrongly_certifies_metamer():
    # train the ordinary correction matrix on three independent color cards
    cards=[[1,0,0,0],[0,1,0,0],[0,0,1,0]]
    train=[(cm.matvec(cm.TOY['camera'],s),cm.matvec(cm.TOY['eye'],s)) for s in cards]
    predict=cm.fit_camera_matrix(train)
    yA=cm.matvec(cm.TOY['camera'],cm.TOY['sA'])
    yB=cm.matvec(cm.TOY['camera'],cm.TOY['sB'])
    # training card sA is fit exactly ...
    assert predict(yA)==pytest.approx([1,1,1])
    # ... but the metamer sB receives the same "brand color ok" verdict while
    # its true observer color is (0,0,2), not (1,1,1)
    assert predict(yB)==pytest.approx([1,1,1])
    assert cm.matvec(cm.TOY['eye'],cm.TOY['sB'])==[0,0,2]


def test_camera_only_evidence_cannot_bound_observer_color():
    yB=cm.matvec(cm.TOY['camera'],cm.TOY['sB'])
    res=cm.certify({'rows':cm.TOY['camera']},cm.TOY['eye'],yB,[0,0,0],
                   None,[1,1,1],0.05,[1,1,1],grid=TOY_GRID)
    # the certificate must expose the whole observer ambiguity, not a point
    assert res['observer_box']==[(0.0,1.0),(0.0,1.0),(1.0,2.0)]
    assert res['status']=='rejected' and res['worst_bound']>1.0


def test_a_fourth_fixed_probe_resolves_only_the_toy_not_the_field_case():
    # on the 4-band toy one more probe does distinguish sA from sB
    M4={'rows':cm.TOY['camera']+[[0,0,0,1]]}
    y=cm.matvec(M4['rows'],cm.TOY['sB'])
    res=cm.certify(M4,cm.TOY['eye'],y,[0,0,0,0],None,[0,0,2],0.05,[1,1,1],grid=TOY_GRID)
    assert res['status']=='approved'
    # but fixed broad filters still cannot separate two shifted narrow peaks
    peak=load('narrow-peak.json')
    M=cm.band_matrix(peak['filter_bands_nm'],peak['grid_nm'])
    ra=cm.matvec(M,peak['peak_a_spectrum']);rb=cm.matvec(M,peak['peak_b_spectrum'])
    assert ra==pytest.approx(rb)
    assert peak['eye_a']!=pytest.approx(peak['eye_b'])


def test_narrow_peak_between_filters_requires_finer_sampling():
    grid=cm.GRID
    eye=[cm.gaussian(grid,460,60),cm.gaussian(grid,550,60),cm.gaussian(grid,620,60)]
    broad=[[400,475],[475,575],[575,700]]
    p1=cm.gaussian(grid,520,4);p2=cm.gaussian(grid,530,4)
    Mb=cm.band_matrix(broad,grid)
    assert cm.matvec(Mb,p1)==pytest.approx(cm.matvec(Mb,p2))
    target=cm.matvec(eye,p1)
    coarse=cm.certify(broad,eye,cm.matvec(Mb,p1),[1e-3]*3,None,target,0.05,[1,1,1])
    # coarse overlapping filters cannot partition the spectrum: the instrument
    # is told to resample rather than handed a false color promise
    assert coarse['status'] in ('rejected','resample')
    if coarse['status']=='rejected':assert coarse['worst_bound']>0.5
    # a fine spectrometer distinguishes them and yields a bound an order of
    # magnitude tighter than the ambiguous broad-filter reading
    fine=[[400+5*i,405+5*i] for i in range(61)]
    good=cm.certify(fine,eye,cm.matvec(cm.band_matrix(fine,grid),p1),
                    [1e-4]*61,None,target,0.05,[1,1,1])
    assert good['status']=='approved' and good['worst_bound']<0.05
    if coarse['status']=='rejected':
        assert good['worst_bound']<coarse['worst_bound']/5


def test_sampling_plan_rejects_instrument_coarser_than_drift():
    coarse=cm.sampling_plan(None,drift_nm=10,min_bandwidth_nm=100,tolerance=0.05)
    assert coarse['verdict']=='resample'
    fine=cm.sampling_plan(None,drift_nm=10,min_bandwidth_nm=10,tolerance=0.05)
    assert fine['verdict']=='capable' and fine['coverage_gaps_nm']==[]


def test_unmeasured_supported_band_is_never_assumed_zero():
    grid=cm.GRID
    eye=[cm.gaussian(grid,550,60)]
    # instrument covers only the first half of the vendor-supported range
    bands=[[400+10*i,410+10*i] for i in range(15)]
    readings=cm.matvec(cm.band_matrix(bands,grid),cm.gaussian(grid,460,30))
    res=cm.certify(bands,eye,readings,[1e-4]*15,[[400,700]],[0.5],0.05,[1.0],grid=grid)
    assert res['status']=='resample' and res['reason']=='unmeasured_supported_band'


# --------------------------------------------------------------------------
# physics: hardware-realizable LUT
# --------------------------------------------------------------------------
PANEL=load('panel.json');OBS=load('observer.json');EYE=OBS['eye_rows'];GRID=OBS['grid_nm']
TARGET=load('target-primary.json')


def test_lut_covers_every_tested_envelope_cell():
    lut=load('lut-primary.json')
    cells={(c['temperature_c'],c['drive_level']) for c in lut['cells']}
    expected={(t,d) for t in PANEL['envelope']['temperatures_c']
              for d in PANEL['envelope']['drive_levels']}
    assert cells==expected
    assert all(0<=code<=2**lut['bits']-1 for c in lut['cells'] for code in c['codes'])
    assert lut['worst_bound']<=TARGET['tolerance']


def test_highlight_outside_gamut_concedes_brightness_not_color_truth():
    doc=load('lut-bright-concession.json')
    assert doc['luminance_scale']<1.0
    assert all(0<=c<=255 for c in doc['codes'])
    assert doc['bound']<=0.18
    # the promised tristimulus is the dimmed target, never an unrealizable RGB
    assert doc['promised_tristimulus']==pytest.approx(
        [t*doc['luminance_scale'] for t in doc['target_tristimulus']],abs=1e-9)


def test_negative_hue_target_is_refused():
    cell,err=cm.realize_lut_cell(PANEL,EYE,[-1.0,0.0,0.0],25,1.0,8,3.0,0.5,[1,1,1],0.5,grid=GRID)
    assert cell is None and err=='target_outside_gamut_hue'


def test_tight_tolerance_cannot_be_satisfied():
    lut,failures=cm.build_lut(PANEL,EYE,TARGET['tristimulus'],PANEL['envelope'],
                              PANEL['bits'],PANEL['drift_nm'],PANEL['quantization'],
                              TARGET['scale'],0.001,grid=GRID)
    assert lut is None and failures and all(f['reason']=='out_of_gamut_or_drift' for f in failures)


# --------------------------------------------------------------------------
# ledger workflow
# --------------------------------------------------------------------------
def setup_ledger(tmp):
    db=tmp/'state.sqlite'
    creative.register(db,FIX/'creative-ok.json')
    creative.license(db,FIX/'license-ok.json')
    schedule.screen(db,FIX/'screen-ok.json')
    colorpipe.import_calibration(db,COLOR/'calibration-ok.json')
    colorpipe.import_calibration(db,COLOR/'calibration-camera.json')
    colorpipe.import_panel(db,COLOR/'panel.json')
    rev2=json.loads((FIX/'creative-ok.json').read_text());rev2['revision']=2
    creative.register(db,write(tmp,'creative2.json',rev2))
    colorpipe.import_target(db,COLOR/'target-primary.json')
    colorpipe.import_target(db,COLOR/'target-fallback.json')
    for i,cap in enumerate(load('captures.json')):
        colorpipe.import_capture(db,write(tmp,f'capture-{i}.json',cap))
    return db


def approval_doc(route='primary',aid=None):
    return {'id':aid or f'AP-AD1-2-P1-{route}','creative_id':'AD1','revision':2,
            'panel_id':'P1','route':route,'grid_nm':GRID,'eye_rows':EYE,
            'spectrometer_error_budget':1e-4}


def booking(doc_id,route,start='2026-06-20T10:00:00Z'):
    return {'id':doc_id,'creative_id':'AD1','revision':2,'screen_id':'S1',
            'start_at':start,'end_at':'2026-06-20T10:10:00Z','priority':10,
            'color_route':route}


def test_primary_approval_needs_full_envelope_and_realizable_lut(tmp_path):
    db=setup_ledger(tmp_path)
    out=colorpipe.certify_revision(db,write(tmp_path,'approval.json',approval_doc()))
    assert out['status']=='approved' and out['worst_bound']<=out['tolerance']
    row=report(db)['state']['color_approval'][0]
    lut=json.loads(row['lut_json'])
    assert len(lut['observer_boxes'])==9


def test_expired_spectrometer_calibration_blocks_primary_claim(tmp_path):
    db=setup_ledger(tmp_path)
    # replace the good calibration with an expired one on every capture
    colorpipe.import_calibration(db,COLOR/'calibration-expired.json')
    caps=load('captures.json')
    for i,cap in enumerate(caps):
        cap=dict(cap);cap['calibration_certificate']='CAL-SPEC-OLD';cap['id']=cap['id']+'-X'
        with pytest.raises(ValueError,match='instrument_calibration_expired'):
            colorpipe.import_capture(db,write(tmp_path,f'capx-{i}.json',cap))


def test_missing_envelope_cell_forces_resample(tmp_path):
    db=setup_ledger(tmp_path)
    # delete the hottest/brightest capture row then recertify on a fresh approval
    with colorpipe.connect(db) as ldb:
        ldb.execute("DELETE FROM capture WHERE temperature_c=45 AND drive_level=1.0")
        ldb.commit()
    out=colorpipe.certify_revision(db,write(tmp_path,'approval2.json',approval_doc(aid='AP2')))
    assert out['status']=='resample' and out['reason']=='envelope_not_covered'


def test_fallback_route_is_distinct_and_keeps_full_brightness(tmp_path):
    db=setup_ledger(tmp_path)
    colorpipe.certify_revision(db,write(tmp_path,'p.json',approval_doc()))
    fb=colorpipe.certify_revision(db,write(tmp_path,'fb.json',approval_doc('fallback','AP-FB')))
    assert fb['status']=='approved_fallback'
    row=[r for r in report(db)['state']['color_approval'] if r['route']=='fallback'][0]
    assert json.loads(row['lut_json'])['brightness_commitment']=='full'


def test_booking_requires_fixed_route_before_confirmation(tmp_path):
    db=setup_ledger(tmp_path)
    colorpipe.certify_revision(db,write(tmp_path,'p.json',approval_doc()))
    # no route named even though targets exist
    with pytest.raises(ValueError,match='color_route_required'):
        schedule.book(db,write(tmp_path,'b0.json',{**booking('B0','primary'),'color_route':None}))
    # fallback target exists but has no approval yet, so naming it is refused
    with pytest.raises(ValueError,match='color_approval_missing|color_route_not_approved'):
        schedule.book(db,write(tmp_path,'b1.json',booking('B1','fallback')))
    colorpipe.certify_revision(db,write(tmp_path,'fb.json',approval_doc('fallback','AP-FB')))
    ok=schedule.book(db,write(tmp_path,'b2.json',booking('B2','fallback')))
    assert ok['status']=='confirmed' and ok['color_route']=='fallback'


def test_revocation_freezes_future_booking_and_notifies(tmp_path):
    db=setup_ledger(tmp_path)
    colorpipe.certify_revision(db,write(tmp_path,'p.json',approval_doc()))
    schedule.book(db,write(tmp_path,'b.json',booking('B2','primary')))
    rev={'id':'RV1','approval_id':'AP-AD1-2-P1-primary',
         'reason':'spectrometer recalibration disproves certificate',
         'measured_at':'2026-06-19T00:00:00Z'}
    out=colorpipe.revoke_approval(db,write(tmp_path,'rev.json',rev))
    assert out['frozen_bookings']==['B2']
    state=report(db)['state']
    assert next(b for b in state['booking'] if b['id']=='B2')['status']=='color_frozen'
    assert state['color_freeze'][0]['status']=='frozen'
    assert state['notification'][0]['kind']=='color_approval_revoked'
    # the revoked primary route can no longer be booked; fallback remains legal
    with pytest.raises(ValueError,match='color_approval_missing'):
        schedule.book(db,write(tmp_path,'b3.json',booking('B3','primary')))
    assert audit(db)['replay_ok']


def test_replay_is_deterministic_across_all_color_events(tmp_path):
    db=setup_ledger(tmp_path)
    colorpipe.certify_revision(db,write(tmp_path,'p.json',approval_doc()))
    colorpipe.certify_revision(db,write(tmp_path,'fb.json',approval_doc('fallback','AP-FB')))
    schedule.book(db,write(tmp_path,'b.json',booking('B2','primary')))
    colorpipe.revoke_approval(db,write(tmp_path,'rev.json',{
        'id':'RV1','approval_id':'AP-AD1-2-P1-primary','reason':'recalibration',
        'measured_at':'2026-06-19T00:00:00Z'}))
    assert audit(db)['replay_ok']


def test_idempotent_reimport_keeps_single_record(tmp_path):
    db=setup_ledger(tmp_path)
    again=colorpipe.import_panel(db,COLOR/'panel.json')
    assert again.get('replayed') is True
    assert len(report(db)['state']['panel'])==1


def test_cli_demo_runs_and_replays(tmp_path):
    env={**__import__('os').environ,'PYTHONPATH':str(ROOT)}
    p=subprocess.run([sys.executable,'-B','-m','adflight','demo','--db',str(tmp_path/'cli.sqlite')],
                     env=env,capture_output=True,text=True,cwd=str(ROOT))
    assert p.returncode==0,p.stderr
    out=json.loads(p.stdout)
    assert out['color']['primary']['status']=='approved'
    q=subprocess.run([sys.executable,'-B','-m','adflight','audit','--db',str(tmp_path/'cli.sqlite')],
                     env=env,capture_output=True,text=True,cwd=str(ROOT))
    assert json.loads(q.stdout)['replay_ok']
