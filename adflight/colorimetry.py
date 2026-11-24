"""Offline panel capture and per-revision color certification.

A camera sRGB reading is not a human-eye color.  The camera filters and the CIE
observer functions are different linear operators on the same emitted spectrum,
so two physically different spectra can produce identical camera values while
the observer sees different colors.  Nothing fit on camera data alone can ever
close that null space: least squares, more color-card shots and extra fixed
probes only move the problem.  Certification therefore works on the *spectrum*
and the certificate is a bound over every spectrum consistent with the actual
instrument readings, the vendor spectral-support / drift envelope and the
instrument error budget -- never an assumption that unmeasured bands are zero.
"""
import math,itertools
from datetime import datetime

GRID=[400.0+5.0*i for i in range(61)]  # 400..700 nm, 5 nm pitch


# --------------------------------------------------------------------------
# small linear-algebra helpers (stdlib only)
# --------------------------------------------------------------------------
def matvec(A,x):
    return [sum(A[i][j]*x[j] for j in range(len(x))) for i in range(len(A))]

def transpose(A):
    return [list(col) for col in zip(*A)]

def matmul(A,B):
    return [[sum(A[i][k]*B[k][j] for k in range(len(B))) for j in range(len(B[0]))] for i in range(len(A))]

def invert(A):
    n=len(A);M=[[float(A[i][j]) for j in range(n)]+[1.0 if i==j else 0.0 for j in range(n)] for i in range(n)]
    for col in range(n):
        pivot=max(range(col,n),key=lambda r:abs(M[r][col]))
        if abs(M[pivot][col])<1e-12:raise ValueError('singular_matrix')
        M[col],M[pivot]=M[pivot],M[col]
        d=M[col][col];M[col]=[v/d for v in M[col]]
        for r in range(n):
            if r==col:continue
            f=M[r][col];M[r]=[M[r][k]-f*M[col][k] for k in range(2*n)]
    return [row[n:] for row in M]

def solve(A,b):
    return matmul(invert(A),b) if isinstance(b[0],list) else matvec(invert(A),b)

def gaussian(wavelengths,center,sigma,peak=1.0):
    return [peak*math.exp(-0.5*((w-center)/sigma)**2) for w in wavelengths]

def norm(v):
    return math.sqrt(sum(x*x for x in v))

def dot(a,b):
    return sum(x*y for x,y in zip(a,b))


# --------------------------------------------------------------------------
# vendor spectral envelope
# --------------------------------------------------------------------------
def support_mask(support,grid=GRID):
    """Boolean mask of grid points where the vendor allows emitted power.

    support is None / 'full' for the whole grid, or a list of [lo,hi] nm
    windows.  Anything outside the windows is contractually dark.
    """
    if not support or support=='full':return [True]*len(grid)
    mask=[False]*len(grid)
    for lo,hi in support:
        for i,w in enumerate(grid):
            if lo-1e-9<=w<=hi+1e-9:mask[i]=True
    return mask


def band_matrix(bands,grid=GRID):
    """Rows are instrument channel response functions over the grid.

    Each band is [lo_nm,hi_nm,weight]; a flat top-hat response is used.  The
    toy four-band counterexample hands explicit row vectors instead, which are
    passed through unchanged (dict form {'rows':[...]}) so the exact textbook
    numbers are reproduced.
    """
    if isinstance(bands,dict) and 'rows' in bands:
        return [list(map(float,row)) for row in bands['rows']]
    rows=[]
    for band in bands:
        lo,hi=float(band[0]),float(band[1]);w=float(band[2]) if len(band)>2 else 1.0
        # half-open [lo,hi): adjacent windows are an exact partition and no
        # trailing window silently swallows the rest of the grid
        rows.append([w if lo-1e-9<=g<hi-1e-9 else 0.0 for g in grid])
    return rows


# --------------------------------------------------------------------------
# feasible spectral set:  s>=0, support mask, |M s - y| <= epsilon (per row)
# --------------------------------------------------------------------------
def _polytope_vertices(A,b):
    """Enumerate vertices of {x : A x <= b} for small dimensions.

    A polytope vertex in n dimensions is the unique solution of n active,
    linearly independent constraint hyperplanes.  The toy has n=4, so testing
    the combinations is exact and tiny; fine-grid panels use the disjoint-band
    partition routine instead.
    """
    n=len(A[0]);verts=[]
    for combo in itertools.combinations(range(len(A)),n):
        K=[A[k] for k in combo]
        try:x=solve(K,[b[k] for k in combo])
        except ValueError:continue
        if all(dot(A[k],x)<=b[k]+1e-9 for k in range(len(A))):
            if not any(norm([x[j]-v[j] for j in range(n)])<1e-9 for v in verts):
                verts.append(x)
    return verts


def polytope_response_interval(eye_row,M,y,eps,mask):
    """Exact min/max of an observer row over the feasible spectral set.

    Feasible set = {s>=0, support mask, y-eps <= M s <= y+eps}.  A linear
    functional reaches its extrema at polytope vertices, enumerated exactly
    here.  Used on small n (the 4-band toy); fine-grid panels use
    partition_response_interval, which is LP-tight on the band partition.
    """
    n=len(mask);A=[];b=[]
    for i,row in enumerate(M):
        A.append(list(row));b.append(y[i]+eps[i])
        A.append([-v for v in row]);b.append(-(y[i]-eps[i]))
    for j in range(n):
        if mask[j]:
            z=[0.0]*n;z[j]=-1.0;A.append(z);b.append(0.0)
        else:
            z=[0.0]*n;z[j]=1.0;A.append(z);b.append(0.0)
            z=[0.0]*n;z[j]=-1.0;A.append(z);b.append(0.0)
    verts=_polytope_vertices(A,b)
    vals=[dot(eye_row,v) for v in verts]
    return min(vals),max(vals)


def partition_response_interval(eye_row,M,y,eps,mask):
    """Tight observer interval when instrument bands partition the grid.

    Full-spectrum field instruments integrate disjoint wavelength windows.
    Adjacent top-hat windows share a boundary cell; the boundary is assigned to
    the first band that sees it.  Within one block all cells share one reading,
    one support mask and one observer coefficient range, so power is pushed to
    the cell with the extremal eye coefficient.  This is LP-tight and scales to
    the full grid without a generic solver.  Rows that genuinely overlap (the
    toy's camera rows share interior band 4) cannot be partitioned.
    """
    n=len(mask)
    # genuine row overlap (the toy's camera rows share interior band 4)
    # cannot be represented as a band partition at all
    seen=[set() for _ in range(n)]
    for i,row in enumerate(M):
        for j,a in enumerate(row):
            if abs(a)>1e-15:seen[j].add(i)
    if any(len(s)>1 for s in seen):
        raise ValueError('overlapping_bands_require_generic_solver')
    owner=[-1]*n
    for i,row in enumerate(M):
        for j,a in enumerate(row):
            if abs(a)>1e-15 and owner[j]==-1:owner[j]=i
    lo_total,hi_total=0.0,0.0
    for i,row in enumerate(M):
        cols=[j for j in range(n) if owner[j]==i]
        if not cols:continue
        w=row[cols[0]]
        if any(abs(row[j]-w)>1e-12 for j in cols):
            raise ValueError('overlapping_bands_require_generic_solver')
        block_lo=max(0.0,y[i]-eps[i]);block_hi=y[i]+eps[i]
        active=[j for j in cols if mask[j]]
        if not active:
            # vendor says this window is dark; a positive reading is a conflict
            if block_hi>1e-12:raise ValueError('reading_outside_vendor_support')
            continue
        cmin=min(eye_row[j] for j in active);cmax=max(eye_row[j] for j in active)
        lo_total+=block_lo*cmin if cmin>=0 else block_hi*cmin
        hi_total+=block_hi*cmax if cmax>=0 else block_lo*cmax
    # uncovered cells are bounded at zero only when the vendor mask forbids
    # them; never assume an unmeasured supported band is zero.
    for j,m in enumerate(mask):
        if owner[j]==-1 and m:
            raise ValueError('unmeasured_supported_band')
    return lo_total,hi_total


def response_box(eye,M,y,eps,mask):
    """Observer tristimulus box [lo,hi] per channel over all feasible spectra."""
    intervals=[]
    for v in eye:
        try:
            intervals.append(partition_response_interval(v,M,y,eps,mask))
        except ValueError as exc:
            if exc.args[0] in ('unmeasured_supported_band','reading_outside_vendor_support'):
                raise
            # overlapping rows with non-uniform weights: only the small
            # combinatorial case (the four-band toy) is enumerated exactly;
            # a fine grid in that shape is not certifiable by this instrument
            if len(mask)>8:
                raise ValueError('overlapping_bands_require_generic_solver')
            intervals.append(polytope_response_interval(v,M,y,eps,mask))
    return intervals


def worst_case_distance(box,target,scale):
    """Largest scaled Euclidean observer distance inside the response box."""
    total=0.0
    for (lo,hi),t,s in zip(box,target,scale):
        d=max(abs(lo-t),abs(hi-t));total+=(d/s)**2
    return math.sqrt(total)


def box_center(box):
    return [(lo+hi)/2.0 for lo,hi in box]


# --------------------------------------------------------------------------
# the textbook counterexample, kept as a numeric witness
# --------------------------------------------------------------------------
TOY=dict(
    camera=[[1,0,0,1],[0,1,0,1],[0,0,1,0]],
    eye=[[1,0,0,0],[0,1,0,0],[0,0,1,1]],
    sA=[1,1,1,0],sB=[0,0,1,1],
)

def toy_witness():
    """Return the full numeric witness: same camera, different observer color."""
    t=TOY
    camA=matvec(t['camera'],t['sA']);camB=matvec(t['camera'],t['sB'])
    eyeA=matvec(t['eye'],t['sA']);eyeB=matvec(t['eye'],t['sB'])
    # null vector of the camera matrix that the observer still sees
    null=[1,1,0,-1]
    assert norm(matvec(t['camera'],null))<1e-12
    seen=matvec(t['eye'],null)
    return {'camera_A':camA,'camera_B':camB,'eye_A':eyeA,'eye_B':eyeB,
            'null_spectrum':null,'observer_sees_null':seen,
            'camera_identical':camA==camB,'eye_different':eyeA!=eyeB}


def fit_camera_matrix(train):
    """Ordinary 3x3 camera->observer least-squares correction matrix.

    train = list of (camera_reading, observer_tristimulus) measured on the
    training screen's color cards.  This is exactly the 'standard' pipeline;
    it is exposed so tests can show it signing off the metamer sB, whose
    camera reading matches a training card but whose observer color differs.
    Least squares cannot remove a null space that the training data never
    excites.
    """
    X=[list(a) for a,_ in train]          # m x 3 camera rows
    Y=[list(b) for _,b in train]          # m x 3 observer rows
    XtX=[[sum(X[k][i]*X[k][j] for k in range(len(X))) for j in range(3)] for i in range(3)]
    XtY=[[sum(X[k][i]*Y[k][r] for k in range(len(X))) for r in range(3)] for i in range(3)]
    Kt=solve(XtX,XtY)                     # columns of K solved together
    K=transpose(Kt)
    def predict(cam):
        return matvec(K,list(cam))
    return predict


# --------------------------------------------------------------------------
# sampling plan: band placement vs vendor drift and instrument resolution
# --------------------------------------------------------------------------
def sampling_plan(support,drift_nm,min_bandwidth_nm,tolerance,grid=GRID):
    """Decide whether a proposed instrument can ever bound the target.

    A narrow emitter that drifts by drift_nm can sit anywhere inside a
    sampling band, so the observer coefficient spread inside the band sets an
    irreducible uncertainty.  If that spread alone exceeds tolerance no reading
    density can rescue the instrument: resample with a finer spectrometer or
    reject.  Otherwise return a uniform band layout whose pitch is fine enough
    that every vendor support window is covered (no unmeasured supported band).
    """
    mask=support_mask(support,grid)
    pitch=max(min_bandwidth_nm,drift_nm)
    bands=[];covered=[False]*len(grid)
    lo=min(g for g,m in zip(grid,mask) if m);hi=max(g for g,m in zip(grid,mask) if m)
    w=lo
    while w<=hi+1e-9:
        bands.append([w,w+pitch]);w+=pitch
    M=band_matrix(bands,grid)
    for row in M:
        for j in range(len(grid)):
            if row[j]:covered[j]=True
    gaps=[grid[j] for j in range(len(grid)) if mask[j] and not covered[j]]
    # A band wider than the drift bound leaves an unresolved in-band peak
    # position (the between-filter narrow-peak failure), so even full coverage
    # is not sufficient: the instrument resolution must dominate the drift.
    fine_enough=min_bandwidth_nm<=drift_nm+1e-9
    return {'pitch_nm':pitch,'bands':bands,'coverage_gaps_nm':gaps,
            'instrument_capable':not gaps and fine_enough,
            'verdict':'resample' if gaps or not fine_enough else 'capable'}


def narrow_peak_drift_penalty(eye_row,center,sigma,drift_nm,grid=GRID):
    """Relative observer-response spread of a narrow peak over its drift range.

    Two peaks shifted within drift_nm can give identical fixed-filter readings
    while this spread changes the observer integral.  The returned number is
    the spread divided by the on-axis response, i.e. the *fractional* observer
    uncertainty of one emitter, which the LUT then scales by that emitter's
    actual emitted tristimulus at the operating point.
    """
    vals=[]
    w=center-drift_nm
    while w<=center+drift_nm+1e-9:
        vals.append(dot(eye_row,gaussian(grid,w,sigma)));w+=grid[1]-grid[0]
    base=dot(eye_row,gaussian(grid,center,sigma))
    if abs(base)<1e-15:return max(vals)-min(vals)
    return (max(vals)-min(vals))/abs(base)


# --------------------------------------------------------------------------
# panel emission model and hardware-realizable LUT
# --------------------------------------------------------------------------
def emitter_spectrum(grid,emitters,temperature_c,drive_level):
    """Forward model: sum of RGB emitters, each shifted by thermal drift."""
    s=[0.0]*len(grid)
    for e in emitters:
        shift=e.get('drift_nm_per_c',0.0)*(temperature_c-e.get('ref_temp_c',temperature_c))
        scale=drive_level*e.get('peak',1.0)*(1.0-e.get('drop_per_c',0.0)*(temperature_c-e.get('ref_temp_c',temperature_c)))
        g=gaussian(grid,e['center_nm']+shift,e['sigma_nm'])
        for j in range(len(grid)):s[j]+=scale*g[j]
    return s


def _channel_unit_spectrum(panel,grid,temperature_c,drive_level,ch,fraction):
    """Spectrum of one emitter at code fraction (0..1), envelope drive fixed."""
    e=dict(panel['emitters'][ch]);e['peak']=e.get('peak',1.0)*fraction
    return emitter_spectrum(grid,[e],temperature_c,drive_level)


def _cone_at(panel,eye,temperature_c,drive_level,grid):
    """Observer tristimulus per channel at full code for one envelope cell."""
    cols=[]
    for ch in range(len(panel['emitters'])):
        sj=_channel_unit_spectrum(panel,grid,temperature_c,drive_level,ch,1.0)
        cols.append(matvec(eye,sj))
    return cols


def realize_lut_cell(panel,eye,target,temperature_c,drive_level,bits,
                     drift_nm,quantization,scale,tolerance,grid=GRID):
    """Find hardware codes whose observer output is within tolerance.

    The measured emitter cone at this (temperature, drive) envelope cell maps
    non-negative code fractions to observer tristimulus.  If the target is
    outside the gamut the fractions are uniformly scaled down -- a real
    luminance concession that preserves the hue/brand ratios.  Fractions are
    snapped to integer codes, then the one-code quantization error and the
    vendor thermal-drift observer error are added to the realized residual.  A
    cell that cannot meet tolerance even at reduced brightness is rejected:
    the panel never receives a mathematically pretty RGB it cannot emit.
    """
    max_code=2**bits-1
    cone=_cone_at(panel,eye,temperature_c,drive_level,grid)
    C=[[cone[ch][r] for ch in range(3)] for r in range(3)]  # observer rows x channel cols
    try:
        weights=solve(C,list(target))
    except ValueError:
        return None,'singular_gamut'
    fractions=list(weights)
    luminance_scale=1.0
    overflow=max([0.0]+fractions)
    if overflow>1.0+1e-9:
        # out-of-gamut high-luminance target: lower actual brightness and
        # judge the color error against the brightness we actually promise
        fractions=[f/overflow for f in fractions];luminance_scale=1.0/overflow
    if min(fractions)<-1e-9:
        return None,'target_outside_gamut_hue'
    promised=[t*luminance_scale for t in target]
    codes=[max(0,min(max_code,int(round(f*max_code)))) for f in fractions]
    s=[0.0]*len(grid)
    for ch,code in enumerate(codes):
        sj=_channel_unit_spectrum(panel,grid,temperature_c,drive_level,ch,code/max_code)
        for j in range(len(grid)):s[j]+=sj[j]
    realized=matvec(eye,s)
    # quantization: each channel may be off by one code
    q_terms=[]
    for ch in range(3):
        sj=_channel_unit_spectrum(panel,grid,temperature_c,drive_level,ch,1.0/max_code)
        q_terms.append([quantization*x for x in matvec(eye,sj)])
    q_error=math.sqrt(sum(max(abs(q[r]) for q in q_terms)**2/scale[r]**2 for r in range(3)))
    # thermal drift: fractional observer spread of each emitter over the
    # vendor drift bound, applied to its realized tristimulus contribution
    d_terms=[0.0,0.0,0.0]
    for ch,e in enumerate(panel['emitters']):
        sj=_channel_unit_spectrum(panel,grid,temperature_c,drive_level,ch,codes[ch]/max_code)
        contrib=matvec(eye,sj)
        for r,row in enumerate(eye):
            frac=narrow_peak_drift_penalty(row,e['center_nm'],e['sigma_nm'],drift_nm,grid)
            d_terms[r]+=frac*abs(contrib[r])
    d_error=math.sqrt(sum(d_terms[r]**2/scale[r]**2 for r in range(3)))
    residual=math.sqrt(sum(((realized[r]-promised[r])/scale[r])**2 for r in range(3)))
    bound=residual+q_error+d_error
    if bound>tolerance:
        return None,'out_of_gamut_or_drift'
    return {'codes':codes,'residual':residual,'quantization_error':q_error,
            'drift_error':d_error,'bound':bound,'luminance_scale':luminance_scale,
            'promised_tristimulus':promised,'realized_tristimulus':realized},None


def build_lut(panel,eye,target,envelope,bits,drift_nm,quantization,scale,tolerance,grid=GRID):
    """LUT over the tested (temperature,drive) envelope; no extrapolation."""
    cells=[];failures=[]
    temps,levels=envelope['temperatures_c'],envelope['drive_levels']
    for tc in temps:
        for dl in levels:
            cell,err=realize_lut_cell(panel,eye,target,tc,dl,bits,drift_nm,quantization,scale,tolerance,grid)
            if err:failures.append({'temperature_c':tc,'drive_level':dl,'reason':err})
            else:
                cell.update(temperature_c=tc,drive_level=dl);cells.append(cell)
    if failures:
        return None,failures
    return {'bits':bits,'temperatures_c':temps,'drive_levels':levels,'cells':cells,
            'envelope':envelope,'worst_bound':max(c['bound'] for c in cells)},None


# --------------------------------------------------------------------------
# evidence-driven certification
# --------------------------------------------------------------------------
def calibration_valid(cal,measured_at):
    start=datetime.fromisoformat(cal['valid_from'][:-1]+'+00:00')
    end=datetime.fromisoformat(cal['valid_until'][:-1]+'+00:00')
    now=datetime.fromisoformat(measured_at[:-1]+'+00:00')
    return start<=now<=end


def certify(camera_bands,eye,readings,eps,support,target,tolerance,scale,grid=None):
    """Provable certificate from instrument readings alone.

    Returns approved/rejected/resample with the observer tristimulus box and
    the worst-case distance over *every* spectrum the evidence allows.
    """
    if grid is None:grid=GRID
    mask=support_mask(support,grid)
    Mcam=band_matrix(camera_bands,grid)
    M=Mcam
    y=list(readings)
    # coverage: a supported grid cell no instrument row sees cannot be bounded
    seen=[any(abs(M[i][j])>1e-15 for i in range(len(M))) for j in range(len(grid))]
    for j in range(len(grid)):
        if mask[j] and not seen[j]:
            return {'status':'resample','reason':'unmeasured_supported_band',
                    'grid_nm':grid[j]}
    try:
        box=response_box(eye,M,y,eps,mask)
    except ValueError as exc:
        if exc.args[0] in ('overlapping_bands_require_generic_solver','unmeasured_supported_band'):
            return {'status':'resample','reason':exc.args[0]}
        raise
    bound=worst_case_distance(box,target,scale)
    center=box_center(box)
    return {'status':'approved' if bound<=tolerance else 'rejected',
            'reason':None if bound<=tolerance else 'observer_bound_exceeds_tolerance',
            'observer_box':box,'worst_bound':bound,'tolerance':tolerance,
            'nominal_tristimulus':center}
