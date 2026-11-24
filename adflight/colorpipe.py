"""Ledger workflow for offline panel capture and color-version certification.

The ledger never treats a camera sRGB value as the on-site human color.  A
primary-route approval requires a currently valid full-spectrum instrument
calibration, spectral captures covering the whole contracted temperature/drive
envelope, and a hardware-realizable LUT whose provable observer-color bound is
inside the contract tolerance.  The fallback route uses pre-approved
monochrome material and is recorded as a distinct route -- it can never be
booked as, or relabeled into, a fulfilled primary route.
"""
import json
from .store import connect,source,already,at
from . import colorimetry as cm

ROUTES=('primary','fallback')


# --------------------------------------------------------------------------
# reference evidence imports
# --------------------------------------------------------------------------
def import_calibration(path,file):
    with connect(path) as db,db:
        sid,d,replay=source(db,'calibration',file)
        if already(db,'calibration',d['id'],sid):return {'id':d['id'],'replayed':True}
        at(d['valid_from']);at(d['valid_until'])
        db.execute('INSERT INTO calibration(id,instrument_serial,kind,valid_from,valid_until,issuer,spec_json,source_id) '
                   'VALUES(?,?,?,?,?,?,?,?)',
                   (d['id'],d['instrument_serial'],d['kind'],d['valid_from'],
                    d['valid_until'],d['issuer'],json.dumps(d.get('spec',{})),sid))
        return {'id':d['id'],'status':'calibrated'}


def import_panel(path,file):
    with connect(path) as db,db:
        sid,d,replay=source(db,'panel',file)
        if already(db,'panel',d['id'],sid):return {'id':d['id'],'replayed':True}
        if not db.execute('SELECT 1 FROM screen WHERE id=?',(d['screen_id'],)).fetchone():
            raise ValueError('unknown_screen')
        version=int(d['version'])
        if db.execute('SELECT 1 FROM panel WHERE screen_id=? AND version=?',(d['screen_id'],version)).fetchone():
            raise ValueError('panel_version_conflict')
        doc={k:v for k,v in d.items() if k not in ('id','screen_id','version')}
        db.execute('INSERT INTO panel(id,screen_id,version,spec_json,source_id) VALUES(?,?,?,?,?)',
                   (d['id'],d['screen_id'],version,json.dumps(doc),sid))
        return {'id':d['id'],'screen_id':d['screen_id'],'version':version,'status':'registered'}


def import_capture(path,file):
    """One offline capture: raw camera, four-band and spectral readings plus
    operating conditions and the calibration certificate used."""
    with connect(path) as db,db:
        sid,d,replay=source(db,'capture',file)
        if already(db,'capture',d['id'],sid):return {'id':d['id'],'replayed':True}
        at(d['measured_at'])
        panel=db.execute('SELECT * FROM panel WHERE id=?',(d['panel_id'],)).fetchone()
        if not panel:raise ValueError('unknown_panel')
        cal=db.execute('SELECT * FROM calibration WHERE id=?',(d['calibration_certificate'],)).fetchone()
        if not cal:raise ValueError('calibration_certificate_missing')
        if not cm.calibration_valid(dict(cal),d['measured_at']):
            raise ValueError('instrument_calibration_expired')
        doc={k:v for k,v in d.items() if k not in ('id','panel_id','measured_at')}
        db.execute('INSERT INTO capture(id,panel_id,measured_at,position,temperature_c,drive_level,'
                   'drive_current_a,ambient_lux,doc_json,source_id) VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (d['id'],d['panel_id'],d['measured_at'],d['position']['sample'],
                    float(d['temperature_c']),float(d['drive_level']),
                    float(d['drive_current_a']),float(d['ambient_lux']),
                    json.dumps(doc),sid))
        return {'id':d['id'],'panel_id':d['panel_id'],'status':'captured',
                'calibration_valid':True}


def import_target(path,file):
    with connect(path) as db,db:
        sid,d,replay=source(db,'color_target',file)
        key=(d['creative_id'],int(d['revision']),d['route'])
        old=db.execute('SELECT source_id FROM color_target WHERE creative_id=? AND revision=? AND route=?',key).fetchone()
        if old:
            if old['source_id']!=sid:raise ValueError('color_target_conflict')
            return {'id':f"{d['creative_id']}:{d['revision']}:{d['route']}",'replayed':True}
        if d['route'] not in ROUTES:raise ValueError('unknown_color_route')
        if not db.execute('SELECT 1 FROM creative WHERE id=? AND revision=?',(d['creative_id'],d['revision'])).fetchone():
            raise ValueError('unknown_creative_revision')
        doc={k:v for k,v in d.items() if k not in ('creative_id','revision','route')}
        db.execute('INSERT INTO color_target(creative_id,revision,route,doc_json,source_id) VALUES(?,?,?,?,?)',
                   (d['creative_id'],d['revision'],d['route'],json.dumps(doc),sid))
        return {'id':f"{d['creative_id']}:{d['revision']}:{d['route']}",'status':'target_registered'}


# --------------------------------------------------------------------------
# certification
# --------------------------------------------------------------------------
def _load(db,table,key_col,key):
    row=db.execute(f'SELECT * FROM {table} WHERE {key_col}=?',(key,)).fetchone()
    return dict(row) if row else None


def _envelope_coverage(captures,envelope):
    """Every contracted (temperature, drive) cell needs its own capture.

    A single success on one calibration chart is not coverage: the continuous
    operating envelope is only certified at cells that were actually tested.
    """
    seen={(c['temperature_c'],c['drive_level']) for c in captures}
    missing=[]
    for tc in envelope['temperatures_c']:
        for dl in envelope['drive_levels']:
            if (float(tc),float(dl)) not in seen:
                missing.append({'temperature_c':tc,'drive_level':dl})
    return missing


def certify_revision(path,file):
    """Issue a color approval for one creative revision on one panel version.

    Input document selects creative/revision, panel, route and the observer +
    tolerance contract.  Primary route: valid spectrometer calibration, full
    envelope spectral coverage, per-capture provable observer boxes inside
    tolerance, and a hardware LUT realizable over every envelope cell.
    Fallback route: pre-approved monochrome artwork; no primary color claim is
    made or implied.
    """
    with connect(path) as db,db:
        sid,d,replay=source(db,'color_approval',file)
        if already(db,'color_approval',d['id'],sid):return {'id':d['id'],'replayed':True}
        creative_id,revision=d['creative_id'],int(d['revision'])
        panel_id,route=d['panel_id'],d['route']
        if route not in ROUTES:raise ValueError('unknown_color_route')
        creative=db.execute('SELECT * FROM creative WHERE id=? AND revision=?',(creative_id,revision)).fetchone()
        if not creative:raise ValueError('unknown_creative_revision')
        panel=db.execute('SELECT * FROM panel WHERE id=?',(panel_id,)).fetchone()
        if not panel:raise ValueError('unknown_panel')
        target_row=db.execute('SELECT * FROM color_target WHERE creative_id=? AND revision=? AND route=?',
                              (creative_id,revision,route)).fetchone()
        if not target_row:raise ValueError('color_target_missing')
        target=json.loads(target_row['doc_json'])
        tolerance=float(target['tolerance']);scale=target['scale']
        tristim=target['tristimulus']
        spec=json.loads(panel['spec_json'])
        status,reason,bound,lut_doc='rejected',None,None,None

        if route=='fallback':
            # distinct, pre-cleared artwork; screen keeps full brightness and
            # no spectral proof is borrowed from the primary route
            status='approved_fallback';reason='preapproved_monochrome_artwork';bound=0.0
            lut_doc={'route':'fallback','brightness_commitment':'full',
                     'artwork_note':target.get('note','')}
        else:
            caps=[dict(r) for r in db.execute('SELECT * FROM capture WHERE panel_id=? ORDER BY measured_at',(panel_id,))]
            if not caps:raise ValueError('no_captures')
            # every capture must trace to a calibration valid at its clock
            eye=d['eye_rows'];grid=d['grid_nm']
            for c in caps:
                cdoc=json.loads(c['doc_json'])
                cal=db.execute('SELECT * FROM calibration WHERE id=?',(cdoc['calibration_certificate'],)).fetchone()
                if not cm.calibration_valid(dict(cal),c['measured_at']):
                    status,reason='rejected','instrument_calibration_expired';break
            else:
                missing=_envelope_coverage(caps,spec['envelope'])
                if missing:
                    status,reason='resample','envelope_not_covered'
                    lut_doc={'missing_cells':missing}
                else:
                    # 1) hardware-realizable LUT over the whole envelope:
                    #    codes, quantization, drift and gamut are decided from
                    #    the measured emitter model before any capture is trusted
                    lut,failures=cm.build_lut(spec,eye,tristim,spec['envelope'],
                                              int(spec['bits']),float(spec['drift_nm']),
                                              float(spec['quantization']),
                                              scale,tolerance,grid=grid)
                    if failures:
                        status,reason='rejected','lut_not_realizable'
                        lut_doc={'failures':failures}
                    else:
                        # 2) each capture is the panel actually driven at its
                        #    envelope cell; certify its spectral observer box
                        #    against the tristimulus the LUT promises THERE
                        #    (a dark cell is never compared to a bright target)
                        budget=float(d.get('spectrometer_error_budget',1e-4))
                        worst=lut['worst_bound'];boxes={}
                        cell_by_key={(cell['temperature_c'],cell['drive_level']):cell
                                     for cell in lut['cells']}
                        for c in caps:
                            cdoc=json.loads(c['doc_json'])
                            cell=cell_by_key[(c['temperature_c'],c['drive_level'])]
                            cert=cm.certify(cdoc['spectrometer_bands_nm'],eye,
                                            cdoc['spectrometer_reading'],
                                            [budget]*len(cdoc['spectrometer_reading']),
                                            spec.get('support'),cell['promised_tristimulus'],
                                            tolerance,scale,grid=grid)
                            if cert['status']=='resample':
                                status,reason='resample',cert['reason'];boxes=None;break
                            if cert['status']=='rejected':
                                status,reason='rejected','capture_outside_tolerance';boxes=None;break
                            boxes[c['id']]=cert['observer_box'];worst=max(worst,cert['worst_bound'])
                        if boxes is not None:
                            status,reason='approved','within_tolerance';bound=worst
                            lut_doc={'lut':lut,'observer_boxes':boxes,
                                     'brightness_concessions':[
                                         {'temperature_c':cell['temperature_c'],
                                          'drive_level':cell['drive_level'],
                                          'luminance_scale':cell['luminance_scale']}
                                         for cell in lut['cells'] if cell['luminance_scale']<1.0-1e-9]}
        db.execute('INSERT INTO color_approval(id,creative_id,revision,panel_id,route,status,reason,'
                   'bound,tolerance,lut_json,source_id,revoked_at,revoke_reason) '
                   'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (d['id'],creative_id,revision,panel_id,route,status,reason,
                    bound,tolerance,json.dumps(lut_doc) if lut_doc is not None else None,
                    sid,None,None))
        return {'id':d['id'],'status':status,'reason':reason,'worst_bound':bound,
                'tolerance':tolerance}


# --------------------------------------------------------------------------
# revocation: freeze not-yet-played windows and notify the advertiser
# --------------------------------------------------------------------------
def revoke_approval(path,file):
    with connect(path) as db,db:
        sid,d,replay=source(db,'revocation',file)
        approval=db.execute('SELECT * FROM color_approval WHERE id=?',(d['approval_id'],)).fetchone()
        if not approval:raise ValueError('unknown_approval')
        if approval['revoked_at'] is not None:
            return {'id':approval['id'],'replayed':True}
        at(d['measured_at'])
        db.execute("UPDATE color_approval SET revoked_at=?,revoke_reason=? WHERE id=?",
                   (d['measured_at'],d['reason'],approval['id']))
        frozen=[]
        if approval['route']=='primary':
            rows=db.execute("SELECT * FROM booking WHERE creative_id=? AND revision=? AND screen_id IN "
                            "(SELECT screen_id FROM panel WHERE id=?) AND status='confirmed' "
                            "AND start_at>=?",(approval['creative_id'],approval['revision'],
                                               approval['panel_id'],d['measured_at'])).fetchall()
            for b in rows:
                fid=f"FZ-{b['id']}"
                if not db.execute('SELECT 1 FROM color_freeze WHERE id=?',(fid,)).fetchone():
                    db.execute('INSERT INTO color_freeze VALUES(?,?,?,?,?,?)',
                               (fid,approval['id'],b['id'],'frozen',1,sid))
                    db.execute("UPDATE booking SET status='color_frozen' WHERE id=?",(b['id'],))
                    db.execute('INSERT INTO notification VALUES(?,?,?,?,?,?)',
                               (f"NTF-{b['id']}",approval['creative_id'],'color_approval_revoked',
                                f"approval {approval['id']} revoked: {d['reason']}; booking {b['id']} frozen",
                                d['measured_at'],sid))
                    frozen.append(b['id'])
        return {'id':approval['id'],'status':'revoked','frozen_bookings':frozen}


def approval_for(db,creative_id,revision,screen_id,route=None):
    """Current, non-revoked approval of a revision on the screen's panel.

    When route is given the approval must be for that exact route, so a
    primary certificate can never satisfy a fallback booking or vice versa.
    """
    sql=("SELECT a.* FROM color_approval a JOIN panel p ON p.id=a.panel_id "
         "WHERE a.creative_id=? AND a.revision=? AND p.screen_id=? "
         "AND a.revoked_at IS NULL AND a.status IN ('approved','approved_fallback')")
    args=[creative_id,revision,screen_id]
    if route is not None:
        sql+=" AND a.route=?";args.append(route)
    return db.execute(sql,args).fetchone()
