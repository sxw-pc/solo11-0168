"""Deterministic builder for the local color-evidence fixtures.

The fixtures are synthetic: no real camera, spectrometer or panel is involved.
Everything is generated from the same forward model used by
adflight.colorimetry, so the raw four-band / full-spectrum readings, the
observer tristimulus values and the panel LUT are mutually consistent and can
be re-derived at any time:

    PYTHONPATH=. python tests/build_color_fixtures.py
"""
import json
from pathlib import Path

from adflight import colorimetry as cm

OUT=Path(__file__).resolve().parent.parent/'fixtures'/'color'
GRID=cm.GRID
# observer: three broad CIE-like response functions on the 400..700 nm grid
EYE=[cm.gaussian(GRID,460,55),cm.gaussian(GRID,545,50),cm.gaussian(GRID,625,55)]
# commercial camera filters: deliberately coarser and offset from the observer
CAMERA_BANDS=[[400,480],[480,570],[570,700]]
# handheld four-band probe: exactly the toy's null-space failure shape
FOUR_BAND={'rows':cm.TOY['camera']}
# rentable full-spectrum spectrometer: 5 nm disjoint windows over the grid;
# half-open [lo,hi), the final window closes one step beyond the last grid point
SPECTRO_BANDS=[[400+5*i,405+5*i] for i in range(61)]
TOY_GRID=[1.0,2.0,3.0,4.0]

PANEL={
    'id':'P1','screen_id':'S1','version':1,
    'drift_nm':3.0,'bits':8,'quantization':0.5,
    'envelope':{'temperatures_c':[5,25,45],'drive_levels':[0.2,0.6,1.0]},
    'emitters':[
        {'center_nm':460,'sigma_nm':12,'peak':1.0,'drift_nm_per_c':0.05,
         'drop_per_c':0.002,'ref_temp_c':25},
        {'center_nm':545,'sigma_nm':12,'peak':1.0,'drift_nm_per_c':0.05,
         'drop_per_c':0.002,'ref_temp_c':25},
        {'center_nm':625,'sigma_nm':14,'peak':1.0,'drift_nm_per_c':0.06,
         'drop_per_c':0.002,'ref_temp_c':25},
    ],
}

CAL_GOOD={'id':'CAL-SPEC-1','instrument_serial':'SPEC-7741','kind':'spectrometer',
          'valid_from':'2026-01-01T00:00:00Z','valid_until':'2027-01-01T00:00:00Z',
          'issuer':'national-metrology-lab'}
CAL_EXPIRED={'id':'CAL-SPEC-OLD','instrument_serial':'SPEC-7741','kind':'spectrometer',
             'valid_from':'2024-01-01T00:00:00Z','valid_until':'2025-01-01T00:00:00Z',
             'issuer':'national-metrology-lab'}
CAL_CAM={'id':'CAL-CAM-1','instrument_serial':'CAM-220','kind':'camera',
         'valid_from':'2026-01-01T00:00:00Z','valid_until':'2027-01-01T00:00:00Z',
         'issuer':'national-metrology-lab'}


def panel_spectrum(temperature_c,drive_level):
    return cm.emitter_spectrum(GRID,PANEL['emitters'],temperature_c,drive_level)


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    dump=lambda name,obj:(OUT/name).write_text(json.dumps(obj,indent=2,sort_keys=True),encoding='utf-8')

    # ---- observer / instrument / panel reference documents ---------------
    dump('observer.json',{'grid_nm':GRID,'eye_rows':EYE,
                          'scale':'per-target relative tristimulus scale',
                          'contract_tolerance':0.2})
    dump('camera.json',{'id':'CAM-220','serial':'CAM-220','bands_nm':CAMERA_BANDS,
                        'calibration_id':'CAL-CAM-1'})
    dump('spectrometer.json',{'id':'SPEC-7741','serial':'SPEC-7741','bands_nm':SPECTRO_BANDS,
                              'error_budget':0.0001,'calibration_id':'CAL-SPEC-1'})
    dump('panel.json',PANEL)
    dump('calibration-ok.json',CAL_GOOD)
    dump('calibration-expired.json',CAL_EXPIRED)
    dump('calibration-camera.json',CAL_CAM)

    # ---- exact four-band toy witness --------------------------------------
    witness=cm.toy_witness()
    dump('toy-witness.json',{
        'grid_bands':TOY_GRID,'camera_rows':cm.TOY['camera'],'eye_rows':cm.TOY['eye'],
        'spectrum_A':cm.TOY['sA'],'spectrum_B':cm.TOY['sB'],**witness,
        'camera_certification_for_B_vs_A_target':cm.certify(
            FOUR_BAND,cm.TOY['eye'],cm.matvec(cm.TOY['camera'],cm.TOY['sB']),
            [0.0,0.0,0.0],None,[1,1,1],0.05,[1,1,1],grid=TOY_GRID),
    })

    # ---- narrow peak that slips between fixed broad filters ---------------
    peak_a=cm.gaussian(GRID,520,4);peak_b=cm.gaussian(GRID,530,4)
    Mcam=cm.band_matrix(CAMERA_BANDS,GRID)
    dump('narrow-peak.json',{
        'grid_nm':GRID,'filter_bands_nm':CAMERA_BANDS,
        'peak_a_center_nm':520,'peak_b_center_nm':530,'sigma_nm':4,
        'peak_a_spectrum':peak_a,'peak_b_spectrum':peak_b,
        'camera_reading_a':cm.matvec(Mcam,peak_a),
        'camera_reading_b':cm.matvec(Mcam,peak_b),
        'eye_a':cm.matvec(EYE,peak_a),'eye_b':cm.matvec(EYE,peak_b),
    })

    # ---- brand targets for the two routes ---------------------------------
    # primary route target: the brand color measured at the brightest
    # envelope cell, so every capture can be certified against it and the LUT
    # only ever concedes actual brightness downward in darker cells
    primary_spectrum=panel_spectrum(25,1.0)
    primary_target=cm.matvec(EYE,primary_spectrum)
    primary_scale=[max(abs(v),1e-9) for v in primary_target]
    dump('target-primary.json',{
        'creative_id':'AD1','revision':2,'route':'primary',
        'tristimulus':primary_target,'tolerance':0.2,'scale':primary_scale,
        'reference_spectrum':primary_spectrum,
        'brightness_commitment':'full, reduced only where the gamut requires it',
    })
    dump('target-fallback.json',{
        'creative_id':'AD1','revision':2,'route':'fallback',
        # approved black/white brand version: neutral axis, lower chroma target
        'tristimulus':[1.2,1.2,1.2],'tolerance':0.2,'scale':[1.2,1.2,1.2],
        'reference_spectrum':None,
        'brightness_commitment':'screen retains full brightness',
        'note':'pre-approved monochrome brand material; different artwork',
    })

    # ---- offline capture set across the full envelope ---------------------
    captures=[]
    for tc in PANEL['envelope']['temperatures_c']:
        for dl in PANEL['envelope']['drive_levels']:
            spectrum=panel_spectrum(tc,dl)
            captures.append({
                'id':f'CAP-{tc}-{str(dl).replace(".","")}',
                'panel_id':'P1','measured_at':f'2026-05-20T{10+int(dl*4):02d}:00:00Z',
                'position':{'x_m':0.5,'y_m':1.2,'sample':'center-patch'},
                'temperature_c':tc,'drive_level':dl,
                'drive_current_a':round(1.8*dl+0.05*(tc-25),4),
                'ambient_lux':320.0,'instrument_serial':'SPEC-7741',
                'camera_serial':'CAM-220',
                'camera_srgb':[max(0,min(255,int(round(v)))) for v in
                               cm.matvec(cm.band_matrix(CAMERA_BANDS,GRID),spectrum)],
                'four_band_reading':cm.matvec(cm.TOY['camera'],[spectrum[0],spectrum[1],spectrum[2],spectrum[3]]),
                'spectrum':spectrum,
                'spectrometer_bands_nm':SPECTRO_BANDS,
                'spectrometer_reading':cm.matvec(cm.band_matrix(SPECTRO_BANDS,GRID),spectrum),
                'observer_tristimulus':cm.matvec(EYE,spectrum),
                'raw_archive':f'raw/CAP-{tc}-{dl}.csv',
                'calibration_certificate':'CAL-SPEC-1',
            })
    dump('captures.json',captures)

    # ---- provable LUT over the tested envelope ----------------------------
    lut,failures=cm.build_lut(PANEL,EYE,primary_target,PANEL['envelope'],
                              PANEL['bits'],PANEL['drift_nm'],PANEL['quantization'],
                              primary_scale,0.2)
    assert failures is None,failures
    dump('lut-primary.json',lut)

    # ---- an over-gamut bright target the panel must concede on ------------
    bright_target=cm.matvec(EYE,panel_spectrum(25,1.0))
    bright_scale=[max(abs(v),1e-9) for v in bright_target]
    bright_cell,err=cm.realize_lut_cell(PANEL,EYE,bright_target,25,0.2,
                                        PANEL['bits'],PANEL['drift_nm'],
                                        PANEL['quantization'],bright_scale,0.18)
    assert err is None and bright_cell['luminance_scale']<1.0
    bright_cell['target_tristimulus']=bright_target
    bright_cell['note']='highlight exceeds the dark-cell gamut; actual brightness is reduced'
    dump('lut-bright-concession.json',bright_cell)
    print('wrote color fixtures to',OUT)


if __name__=='__main__':
    main()
