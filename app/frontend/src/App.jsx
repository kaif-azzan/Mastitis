import { useCallback, useEffect, useState } from 'react';

const initialHealth = {
  connected: false,
  ready: false,
  verifiedCount: 0,
  thermalMasterInstalled: false,
  thermalOutputDirectory: false,
  devices: [],
};

const initialTopCamera = {
  connected: false,
  streamActive: false,
  configured: false,
  verified: false,
  deviceConnected: false,
  name: '',
  ipAddress: '',
  connectionPort: '',
  socketConnected: false,
  hasFrame: false,
  frameCount: 0,
  transport: 'ADB reverse / localhost',
  controls: {
    ready: false,
    focusSupported: false,
    exposureSupported: false,
    focusMode: 'auto',
    focusPosition: 0.5,
    exposureIndex: 0,
    exposureMin: -6,
    exposureMax: 6,
  },
};

const initialProcessing = {
  decoderAvailable: false,
  decoder: 'Unavailable',
  frameCount: 0,
  session: null,
  activeDetections: [],
  lastEvent: null,
  lastCapture: null,
  visits: [],
};

const QUARTER_OPTIONS = [
  { value: 'left-upper', label: 'Left upper' },
  { value: 'left-lower', label: 'Left lower' },
  { value: 'right-upper', label: 'Right upper' },
  { value: 'right-lower', label: 'Right lower' },
  { value: 'left-eye', label: 'Left eye' },
  { value: 'right-eye', label: 'Right eye' },
];

const STANDARD_CAMERA_COUNT = 5;
const REQUIRED_UDDER_AREAS = ['left-upper', 'left-lower', 'right-upper', 'right-lower'];
const EYE_AREAS = ['left-eye', 'right-eye'];

const SETUP_STORAGE_KEY = 'thermal-master-camera-setup-v1';
const TOP_CAMERA_STORAGE_KEY = 'thermal-master-top-camera-v1';

function createSetupSlot(slot) {
  return {
    slot,
    ipAddress: '',
    pairingPort: '',
    pairingCode: '',
    connectionPort: '',
    quarter: '',
    checkStatus: 'not_checked',
    checkMessage: 'Not checked',
  };
}

function loadSetupSlots() {
  try {
    const stored = window.localStorage.getItem(SETUP_STORAGE_KEY);
    const parsed = stored ? JSON.parse(stored) : null;
    if (Array.isArray(parsed)) {
      const configured = parsed
        .filter((slot) => slot && (slot.ipAddress || slot.pairingPort || slot.pairingCode || slot.connectionPort || slot.quarter || slot.checkStatus === 'verified'))
        .map((slot, index) => {
          const removedQuarter = slot.quarter === 'behind';
          return {
            ...createSetupSlot(index + 1),
            ...slot,
            slot: index + 1,
            quarter: removedQuarter ? '' : slot.quarter,
            ...(removedQuarter ? { checkStatus: 'not_checked', checkMessage: 'Choose a new covered area' } : {}),
          };
        });
      return configured;
    }
  } catch {
    // Keep the setup page usable if browser storage is unavailable or corrupt.
  }
  return [];
}

function loadTopCameraConfig() {
  try {
    const stored = window.localStorage.getItem(TOP_CAMERA_STORAGE_KEY);
    const parsed = stored ? JSON.parse(stored) : null;
    if (parsed && typeof parsed === 'object') {
      return {
        name: parsed.name || '',
        ipAddress: parsed.ipAddress || '',
        pairingPort: parsed.pairingPort || '',
        pairingCode: parsed.pairingCode || '',
        connectionPort: parsed.connectionPort || '',
      };
    }
  } catch {
    // Keep the configuration page usable if browser storage is unavailable or corrupt.
  }
  return { name: '', ipAddress: '', pairingPort: '', pairingCode: '', connectionPort: '' };
}

function setupStatusLabel(status) {
  if (status === 'checking') return 'Checking';
  if (status === 'details_valid') return 'Details valid';
  if (status === 'verified') return 'Verified';
  return 'Not checked';
}

function StatusPill({ good, failed = false, children }) {
  return <span className={`status-pill ${failed ? 'failed' : good ? 'good' : 'muted'}`}>{children}</span>;
}

export default function App() {
  const [page, setPage] = useState('setup');
  const [health, setHealth] = useState(initialHealth);
  const [topCamera, setTopCamera] = useState(initialTopCamera);
  const [processing, setProcessing] = useState(initialProcessing);
  const [sites, setSites] = useState([]);
  const [setupSlots, setSetupSlots] = useState(loadSetupSlots);
  const [topCameraConfig, setTopCameraConfig] = useState(loadTopCameraConfig);
  const [topCameraCheck, setTopCameraCheck] = useState({ status: 'not_checked', message: 'Not checked' });
  const [selectedSite, setSelectedSite] = useState('');
  const [siteCode, setSiteCode] = useState('');
  const [cowCount, setCowCount] = useState('30');
  const [generatorBusy, setGeneratorBusy] = useState(false);
  const [deletingSite, setDeletingSite] = useState('');
  const [sessionBusy, setSessionBusy] = useState(false);
  const [notice, setNotice] = useState(null);
  const [visibleCapture, setVisibleCapture] = useState(null);
  const [testMode, setTestMode] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);

  useEffect(() => {
    window.localStorage.setItem(SETUP_STORAGE_KEY, JSON.stringify(setupSlots));
  }, [setupSlots]);

  useEffect(() => {
    window.localStorage.setItem(TOP_CAMERA_STORAGE_KEY, JSON.stringify(topCameraConfig));
  }, [topCameraConfig]);

  async function syncThermalConfiguration(slots) {
    const verifiedSlots = slots
      .filter((slot) => slot.checkStatus === 'verified' && slot.quarter)
      .map((slot) => slot.slot);
    const response = await fetch('/api/thermal/setup/sync', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ verifiedSlots }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Could not synchronize camera setup');
    return data;
  }

  useEffect(() => {
    // Remove persisted devices from older setup sessions that are no longer
    // represented by the current browser setup.
    syncThermalConfiguration(setupSlots).catch(() => {});
  }, []);

  const processingActive = Boolean(processing.session);
  const processingPaused = Boolean(processing.session?.paused || processing.session?.status === 'paused');
  const verifiedQuarters = setupSlots.filter((slot) => slot.checkStatus === 'verified').map((slot) => slot.quarter);
  const thermalSetupComplete = setupSlots.length === STANDARD_CAMERA_COUNT
    && setupSlots.every((slot) => slot.checkStatus === 'verified' && slot.quarter)
    && new Set(verifiedQuarters).size === STANDARD_CAMERA_COUNT
    && REQUIRED_UDDER_AREAS.every((area) => verifiedQuarters.includes(area))
    && verifiedQuarters.filter((area) => EYE_AREAS.includes(area)).length === 1;
  const verifiedCount = setupSlots.filter((slot) => slot.checkStatus === 'verified').length;
  const topCameraReady = topCamera.configured && topCamera.verified;
  const setupComplete = thermalSetupComplete && topCameraReady;
  const canProceed = setupComplete || (testMode && topCameraReady);
  const setupCameraCount = testMode ? `${setupSlots.length} cameras added` : `${setupSlots.length}/${STANDARD_CAMERA_COUNT} cameras added`;
  const standardSetupMessage = setupSlots.length > STANDARD_CAMERA_COUNT
    ? `Saved setup has ${setupSlots.length} cameras. Standard mode needs 5: four udder areas and one eye area. Remove ${setupSlots.length - STANDARD_CAMERA_COUNT} ${setupSlots.length - STANDARD_CAMERA_COUNT === 1 ? 'camera' : 'cameras'} and recheck the remaining cameras.`
    : setupSlots.length < STANDARD_CAMERA_COUNT
      ? 'Standard mode needs 5 cameras: four udder areas and one eye area.'
      : !setupSlots.every((slot) => slot.checkStatus === 'verified')
        ? 'Check all 5 camera connections.'
        : 'Assign all four udder areas and exactly one eye area.';

  function updateSetupSlot(index, field, value) {
    setSetupSlots((current) => current.map((slot, slotIndex) => (
      slotIndex === index
        ? { ...slot, [field]: value, checkStatus: 'not_checked', checkMessage: 'Configuration changed; check this phone again' }
        : slot
    )));
  }

  function addSetupSlot() {
    if (processingActive || (!testMode && setupSlots.length >= STANDARD_CAMERA_COUNT)) return;
    setSetupSlots((current) => [...current, createSetupSlot(current.length + 1)]);
  }

  async function removeSetupSlot(index) {
    if (processingActive || setupSlots.some((slot) => slot.checkStatus === 'checking')) return;
    // Removing a slot renumbers the remaining slots, so require them to be
    // checked again before they can be used under their new slot numbers.
    const nextSlots = setupSlots
      .filter((_, slotIndex) => slotIndex !== index)
      .map((slot, slotIndex) => ({
        ...slot,
        slot: slotIndex + 1,
        checkStatus: 'not_checked',
        checkMessage: 'Configuration changed; check this phone again',
      }));
    setSetupSlots(nextSlots);
    try {
      await syncThermalConfiguration(nextSlots);
      setNotice({ type: 'success', message: 'Camera removed. Check remaining cameras again.' });
    } catch (error) {
      setNotice({ type: 'error', message: error.message || 'Could not synchronize camera setup' });
    }
  }

  function updateTopCameraConfig(field, value) {
    setTopCameraConfig((current) => ({ ...current, [field]: value }));
    setTopCamera((current) => ({ ...current, configured: false, verified: false }));
    setTopCameraCheck({ status: 'not_checked', message: 'Configuration changed; check this phone again' });
  }

  async function checkTopCamera() {
    const ipValid = /^(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)$/.test(topCameraConfig.ipAddress.trim());
    const portValid = (value) => /^\d{1,5}$/.test(value) && Number(value) >= 1 && Number(value) <= 65535;
    let message = '';
    if (!ipValid) message = 'Enter a valid private IPv4 address.';
    else if (!portValid(topCameraConfig.connectionPort)) message = 'Enter a valid connection port.';
    else if (topCameraConfig.pairingPort && !portValid(topCameraConfig.pairingPort)) message = 'Enter a valid pairing port.';
    else if (topCameraConfig.pairingPort && topCameraConfig.pairingCode && !/^\d{4,8}$/.test(topCameraConfig.pairingCode)) message = 'Enter a valid Android pairing code.';
    if (message) {
      setTopCameraCheck({ status: 'not_checked', message });
      return;
    }

    setTopCameraCheck({ status: 'checking', message: 'Connecting and launching the overhead-camera sender...' });
    setNotice(null);
    try {
      const response = await fetch('/api/top-camera/setup/check', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: topCameraConfig.name.trim() || 'Overhead camera',
          ipAddress: topCameraConfig.ipAddress.trim(),
          pairingPort: topCameraConfig.pairingPort,
          pairingCode: topCameraConfig.pairingCode,
          connectionPort: topCameraConfig.connectionPort,
        }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Overhead camera verification failed');
      setTopCameraCheck({ status: 'verified', message: `${data.message} (${data.model || 'Android phone'}, Android ${data.androidVersion || 'unknown'})` });
      setNotice({ type: 'success', message: 'The selected overhead camera is verified.' });
      await refreshTopCamera();
    } catch (error) {
      setTopCameraCheck({ status: 'not_checked', message: error.message || 'Overhead camera verification failed' });
    }
  }

  async function checkSetupDetails(index) {
    const slot = setupSlots[index];
    const ipValid = /^(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)$/.test(slot.ipAddress.trim());
    const portValid = (value) => /^\d{1,5}$/.test(value) && Number(value) >= 1 && Number(value) <= 65535;
    const duplicateQuarter = !testMode && setupSlots.some((candidate, candidateIndex) => candidateIndex !== index && candidate.quarter && candidate.quarter === slot.quarter);
    let message = '';
    if (!ipValid) message = 'Enter a valid private IPv4 address.';
    else if (!portValid(slot.connectionPort)) message = 'Enter a valid connection port.';
    else if (slot.pairingPort && !portValid(slot.pairingPort)) message = 'Enter a valid pairing port.';
    else if (slot.pairingPort && slot.pairingCode && !/^\d{4,8}$/.test(slot.pairingCode)) message = 'Enter a valid Android pairing code.';
    else if (!slot.quarter) message = 'Choose a covered area.';
    else if (duplicateQuarter) message = 'Each area must be assigned to only one phone.';

    if (message) {
      setSetupSlots((current) => current.map((candidate, candidateIndex) => (
        candidateIndex === index ? { ...candidate, checkStatus: 'not_checked', checkMessage: message } : candidate
      )));
      return;
    }

    setSetupSlots((current) => current.map((candidate, candidateIndex) => (
      candidateIndex === index ? { ...candidate, checkStatus: 'checking', checkMessage: 'Connecting and verifying Thermal Master controls...' } : candidate
    )));
    setNotice(null);
    try {
      const response = await fetch('/api/thermal/setup/check', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          slot: slot.slot,
          ipAddress: slot.ipAddress.trim(),
          pairingPort: slot.pairingPort,
          pairingCode: slot.pairingCode,
          connectionPort: slot.connectionPort,
          quarter: slot.quarter,
          testMode,
        }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Thermal camera verification failed');
      setSetupSlots((current) => current.map((candidate, candidateIndex) => (
        candidateIndex === index
          ? { ...candidate, checkStatus: 'verified', checkMessage: `${data.message} (${data.model || 'Android phone'}, Android ${data.androidVersion || 'unknown'})` }
          : candidate
      )));
      setNotice({ type: 'success', message: `Thermal camera ${slot.slot} is verified.` });
      await refreshHealth();
    } catch (error) {
      setSetupSlots((current) => current.map((candidate, candidateIndex) => (
        candidateIndex === index
          ? { ...candidate, checkStatus: 'not_checked', checkMessage: error.message || 'Thermal camera verification failed' }
          : candidate
      )));
    }
  }

  const refreshHealth = useCallback(async () => {
    try {
      const response = await fetch('/api/health', { cache: 'no-store' });
      setHealth(await response.json());
    } catch {
      setHealth({ ...initialHealth, error: 'Backend is not running' });
    }
  }, []);

  const refreshTopCamera = useCallback(async () => {
    try {
      const response = await fetch('/api/top-camera/status', { cache: 'no-store' });
      const data = await response.json();
      setTopCamera(data);
      setTopCameraConfig((current) => current.ipAddress ? current : ({
        ...current,
        name: data.name || current.name,
        ipAddress: data.ipAddress || current.ipAddress,
        connectionPort: data.connectionPort ? String(data.connectionPort) : current.connectionPort,
      }));
    } catch {
      setTopCamera(initialTopCamera);
    }
  }, []);

  const refreshSites = useCallback(async () => {
    try {
      const response = await fetch('/api/sites', { cache: 'no-store' });
      const data = await response.json();
      setSites(data);
      setSelectedSite((current) => data.some((site) => site.code === current) ? current : data[0]?.code || '');
    } catch {
      setSites([]);
    }
  }, []);

  const refreshProcessing = useCallback(async () => {
    try {
      const response = await fetch('/api/processing/status', { cache: 'no-store' });
      const data = await response.json();
      setProcessing(data);
      if (data.session) {
        // The backend owns session state. Restore the processing view after a
        // browser reload instead of leaving the user stranded on Setup.
        setPage('processing');
        setSelectedSite(data.session.siteCode || '');
        setTestMode(Boolean(data.session.testMode));
      }
      return data;
    } catch {
      setProcessing(initialProcessing);
      return initialProcessing;
    }
  }, []);

  useEffect(() => {
    refreshHealth();
    refreshTopCamera();
    refreshSites();
    refreshProcessing();
    const healthTimer = window.setInterval(refreshHealth, 5000);
    const topCameraTimer = window.setInterval(refreshTopCamera, 1000);
    const processingTimer = window.setInterval(refreshProcessing, 1000);
    const sitesTimer = window.setInterval(refreshSites, 5000);
    return () => {
      window.clearInterval(healthTimer);
      window.clearInterval(topCameraTimer);
      window.clearInterval(processingTimer);
      window.clearInterval(sitesTimer);
    };
  }, [refreshHealth, refreshTopCamera, refreshSites, refreshProcessing]);

  async function generateSite(event) {
    event.preventDefault();
    setGeneratorBusy(true);
    setNotice(null);
    try {
      const response = await fetch('/api/sites/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ siteCode: siteCode.trim(), cowCount: Number(cowCount) }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Could not generate QR codes');
      setNotice({ type: 'success', message: `Generated ${data.cowCount} QR codes for ${data.siteCode}. Saved to: ${data.folder}` });
      setSiteCode('');
      await refreshSites();
    } catch (error) {
      setNotice({ type: 'error', message: error.message || 'Could not generate QR codes' });
    } finally {
      setGeneratorBusy(false);
    }
  }

  async function deleteSite(code) {
    if (processingActive || !window.confirm(`Delete ${code} and all its QR codes, visit records, and captured images? This cannot be undone.`)) return;
    setDeletingSite(code);
    setNotice(null);
    try {
      const response = await fetch(`/api/sites/${encodeURIComponent(code)}`, { method: 'DELETE' });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || `Could not delete site ${code}`);
      setNotice({ type: 'success', message: `Deleted ${code} and its associated data.` });
      await refreshSites();
    } catch (error) {
      setNotice({ type: 'error', message: error.message || `Could not delete site ${code}` });
    } finally {
      setDeletingSite('');
    }
  }

  async function sendTopCameraControl(settings) {
    setTopCamera((current) => ({
      ...current,
      controls: { ...current.controls, ...settings },
    }));
    try {
      const response = await fetch('/api/top-camera/control', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(settings),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Could not update camera controls');
      if (data.controls) {
        setTopCamera((current) => ({ ...current, controls: data.controls }));
      }
    } catch (error) {
      setNotice({ type: 'error', message: error.message || 'Could not update camera controls' });
      await refreshTopCamera();
    }
  }

  function updateExposure(event) {
    const exposureIndex = Number(event.target.value);
    sendTopCameraControl({ exposureIndex });
  }

  function updateFocus(event) {
    const focusPosition = Number(event.target.value) / 100;
    sendTopCameraControl({ focusPosition, autofocus: false });
  }

  function resetFocus() {
    sendTopCameraControl({ autofocus: true });
  }

  async function startSession() {
    if (!canProceed) {
      setNotice({ type: 'error', message: testMode ? 'Verify the overhead camera before starting test mode.' : 'Verify the overhead camera and configure five thermal cameras: four udder areas and one eye area.' });
      setPage('setup');
      return;
    }
    if (!selectedSite) return;
    setSessionBusy(true);
    setNotice(null);
    try {
      // Reconcile persisted devices immediately before starting. This also
      // removes old saved cameras when the configured thermal-camera count changes.
      await syncThermalConfiguration(setupSlots);
      const response = await fetch('/api/processing/sessions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ siteCode: selectedSite, testMode }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Could not start processing');
      setNotice({ type: 'success', message: `Processing session started for ${data.siteCode}.` });
      await refreshProcessing();
    } catch (error) {
      setNotice({ type: 'error', message: error.message || 'Could not start processing' });
    } finally {
      setSessionBusy(false);
    }
  }

  async function stopSession() {
    setSessionBusy(true);
    try {
      const response = await fetch('/api/processing/sessions/stop', { method: 'POST' });
      if (!response.ok) throw new Error('Could not stop the active processing session');
      setNotice({ type: 'success', message: 'Processing stopped. The live feed and QR recognition are off.' });
      await refreshProcessing();
      // Keep the user on Processing so the stop action is predictable. The
      // sidebar becomes available for Setup or QR generation after refresh.
    } catch (error) {
      setNotice({ type: 'error', message: error.message || 'Could not stop the active processing session' });
    } finally {
      setSessionBusy(false);
    }
  }

  async function togglePauseSession() {
    if (!processingActive) return;
    const action = processingPaused ? 'resume' : 'pause';
    setSessionBusy(true);
    setNotice(null);
    try {
      const response = await fetch(`/api/processing/sessions/${action}`, { method: 'POST' });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || `Could not ${action} processing`);
      setNotice({ type: 'success', message: processingPaused ? 'Processing resumed.' : 'Processing paused. The live feed remains active.' });
      await refreshProcessing();
    } catch (error) {
      setNotice({ type: 'error', message: error.message || `Could not ${action} processing` });
    } finally {
      setSessionBusy(false);
    }
  }

  function enableTestMode() {
    if (processingActive) return;
    setTestMode(true);
    setNotice({ type: 'success', message: 'Test mode enabled. Verify at least one phone, then continue with the cameras that are ready.' });
  }

  function toggleTestMode() {
    if (!testMode) {
      enableTestMode();
      return;
    }
    if (setupSlots.length > STANDARD_CAMERA_COUNT) {
      setNotice({ type: 'error', message: `Remove cameras until no more than ${STANDARD_CAMERA_COUNT} remain before exiting test mode.` });
      return;
    }
    setTestMode(false);
  }

  async function overrideVisit() {
    const visitId = processing.lastEvent?.visitId;
    if (!visitId) return;
    setSessionBusy(true);
    try {
      const response = await fetch(`/api/processing/visits/${visitId}/override`, { method: 'POST' });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Could not override visit');
      setNotice({ type: 'success', message: 'Override accepted. The cow is being tracked again.' });
      await refreshProcessing();
    } catch (error) {
      setNotice({ type: 'error', message: error.message || 'Could not override visit' });
    } finally {
      setSessionBusy(false);
    }
  }

  const lastEventType = processing.lastEvent?.type;
  const eventNeedsAction = processingActive && !processingPaused && lastEventType === 'duplicate';
  const multipleQrEvent = processingActive && !processingPaused && lastEventType === 'multiple_qr';
  const wrongSiteEvent = processingActive && !processingPaused && lastEventType === 'wrong_site';
  const cameraControls = topCamera.controls || initialTopCamera.controls;
  const lastCapture = processing.lastCapture;
  const captureInProgress = processingActive && ['scheduled', 'capturing'].includes(lastCapture?.status);
  const captureKey = lastCapture ? `${lastCapture.batchId || ''}:${lastCapture.completedAt || ''}:${lastCapture.status}` : '';

  useEffect(() => {
    if (!lastCapture || !['completed', 'partial'].includes(lastCapture.status)) {
      setVisibleCapture(null);
      return undefined;
    }
    const completedAt = Date.parse(lastCapture.completedAt || '');
    const elapsed = Number.isFinite(completedAt) ? Date.now() - completedAt : 0;
    const remaining = Math.max(0, 10000 - elapsed);
    if (!lastCapture.results?.some((result) => result.status === 'completed') || remaining === 0) {
      setVisibleCapture(null);
      return undefined;
    }
    setVisibleCapture(lastCapture);
    const timer = window.setTimeout(() => setVisibleCapture(null), remaining);
    return () => window.clearTimeout(timer);
  }, [captureKey]);

  function navigatePage(nextPage) {
    if (nextPage === 'processing') setNotice(null);
    setPage(nextPage);
  }

  return (
    <main className={`app-shell ${page === 'processing' ? 'processing-shell' : ''}`}>
      <div className={`app-layout ${sidebarCollapsed ? 'sidebar-collapsed' : ''}`}>
        <aside className={`sidebar ${sidebarCollapsed ? 'collapsed' : ''}`} aria-label="Application navigation">
          <div className="sidebar-heading">
            <button className="sidebar-mark" type="button" onClick={() => setSidebarCollapsed((current) => !current)} aria-label={sidebarCollapsed ? 'Expand navigation' : 'Collapse navigation'} title={sidebarCollapsed ? 'Expand navigation' : 'Collapse navigation'}>TM</button>
            {!sidebarCollapsed && <span className="sidebar-title">Thermal Master</span>}
          </div>
          <nav className="sidebar-nav">
            <button className={`menu-item ${page === 'setup' ? 'selected' : ''}`} type="button" onClick={() => navigatePage('setup')} disabled={processingActive} title="Setup">
              <span className="menu-number">01</span><span className="menu-label">Setup</span>
            </button>
            <button className={`menu-item ${page === 'generator' ? 'selected' : ''}`} type="button" onClick={() => navigatePage('generator')} disabled={processingActive || (!testMode && !canProceed)} title="Generate QR codes">
              <span className="menu-number">02</span><span className="menu-label">Generate QR</span>
            </button>
            <button className={`menu-item ${page === 'processing' ? 'selected' : ''}`} type="button" onClick={() => navigatePage('processing')} disabled={!testMode && !canProceed} title="Processing">
              <span className="menu-number">03</span><span className="menu-label">Processing</span>
            </button>
          </nav>
        </aside>

        <div className="app-content">
          {notice && (page !== 'processing' || notice.type === 'error') && <div className={`notice-banner ${notice.type}`} role="status">{notice.message}</div>}

          {page === 'setup' ? (
        <section className="setup-card" aria-label="Thermal camera setup">
          <div className="setup-heading">
            <div>
              <p className="eyebrow">CAMERAS</p>
              <h2>Camera setup</h2>
            </div>
            <StatusPill good={setupComplete}>{setupComplete ? 'All cameras verified' : `${verifiedCount} verified · ${setupSlots.length}${testMode ? ' added' : `/${STANDARD_CAMERA_COUNT} added`} · ${topCameraReady ? 'top verified' : 'top not verified'}`}</StatusPill>
          </div>

          {processingActive && <div className="setup-recovery" role="alert">
            <div>
              <strong>Session active</strong>
            </div>
            <button className="secondary-button" type="button" onClick={stopSession} disabled={sessionBusy}>{sessionBusy ? 'Stopping...' : 'Stop active session'}</button>
          </div>}

          <div className={`test-mode-banner ${testMode ? 'active' : ''}`} role="status">
            <div><strong>{testMode ? 'TEST MODE' : 'STANDARD MODE'}</strong></div>
            <button className="secondary-button" type="button" onClick={toggleTestMode} disabled={processingActive}>{testMode ? 'Exit test mode' : 'Enable test mode'}</button>
          </div>

          <div className="top-camera-setup">
            <div className="setup-row-heading">
              <div><span className="label">Overhead camera</span><strong>{topCameraConfig.name || 'Not configured'}</strong></div>
              <StatusPill good={topCameraReady}>{topCameraCheck.status === 'checking' ? 'Checking' : topCameraReady ? (topCamera.connected ? 'Verified / live' : 'Verified') : 'Not verified'}</StatusPill>
            </div>
            <div className="top-camera-grid">
              <label>Camera name<input value={topCameraConfig.name} onChange={(event) => updateTopCameraConfig('name', event.target.value)} placeholder="Overhead phone" disabled={processingActive} /></label>
              <label>Phone IP<input value={topCameraConfig.ipAddress} onChange={(event) => updateTopCameraConfig('ipAddress', event.target.value)} placeholder="192.168.100.20" inputMode="decimal" disabled={processingActive} /></label>
              <label>Connection port<input value={topCameraConfig.connectionPort} onChange={(event) => updateTopCameraConfig('connectionPort', event.target.value)} placeholder="ADB port" inputMode="numeric" disabled={processingActive} /></label>
              <label>Pairing code<input type="password" value={topCameraConfig.pairingCode} onChange={(event) => updateTopCameraConfig('pairingCode', event.target.value)} placeholder="6-digit code" inputMode="numeric" disabled={processingActive} /></label>
              <label>Pairing port<input value={topCameraConfig.pairingPort} onChange={(event) => updateTopCameraConfig('pairingPort', event.target.value)} placeholder="Pair port" inputMode="numeric" disabled={processingActive} /></label>
              <button className="secondary-button setup-check-button" type="button" onClick={checkTopCamera} disabled={processingActive || topCameraCheck.status === 'checking'}>{topCameraCheck.status === 'checking' ? 'Checking...' : 'Check overhead camera'}</button>
            </div>
            <p className={`setup-message ${topCameraCheck.status === 'verified' ? 'valid' : ''}`}>{topCameraCheck.message}</p>
          </div>

          <div className="setup-list">
            <div className="setup-list-heading">
              <div><span className="label">Thermal camera phones</span><p>{setupCameraCount}</p></div>
              <button className="secondary-button add-camera-button" type="button" onClick={addSetupSlot} disabled={processingActive || (!testMode && setupSlots.length >= STANDARD_CAMERA_COUNT)}>{!testMode && setupSlots.length >= STANDARD_CAMERA_COUNT ? (setupSlots.length > STANDARD_CAMERA_COUNT ? 'Remove a camera' : `All ${STANDARD_CAMERA_COUNT} cameras added`) : '+ Add thermal camera'}</button>
            </div>
            {!testMode && !thermalSetupComplete && <div className="setup-recovery" role="alert"><strong>{standardSetupMessage}</strong></div>}
            {setupSlots.length === 0 && <div className="setup-empty"><strong>No cameras</strong></div>}
            {setupSlots.map((slot, index) => (
              <article className="setup-row" key={slot.slot}>
                <div className="setup-row-heading">
                  <div><span className="label">Thermal camera {slot.slot}</span><strong>{slot.quarter ? QUARTER_OPTIONS.find((option) => option.value === slot.quarter)?.label : 'Quarter not assigned'}</strong></div>
                  <div className="setup-row-actions"><StatusPill good={slot.checkStatus === 'verified'}>{setupStatusLabel(slot.checkStatus)}</StatusPill><button className="remove-button" type="button" onClick={() => removeSetupSlot(index)} disabled={processingActive || setupSlots.some((candidate) => candidate.checkStatus === 'checking')}>Remove</button></div>
                </div>
                <div className="setup-grid">
                  <label>Phone IP<input value={slot.ipAddress} onChange={(event) => updateSetupSlot(index, 'ipAddress', event.target.value)} placeholder="192.168.100.20" inputMode="decimal" disabled={processingActive} /></label>
                  <label>Connection port<input value={slot.connectionPort} onChange={(event) => updateSetupSlot(index, 'connectionPort', event.target.value)} placeholder="ADB port" inputMode="numeric" disabled={processingActive} /></label>
                  <label>Pairing code<input type="password" value={slot.pairingCode} onChange={(event) => updateSetupSlot(index, 'pairingCode', event.target.value)} placeholder="6-digit code" inputMode="numeric" disabled={processingActive} /></label>
                  <label>Pairing port<input value={slot.pairingPort} onChange={(event) => updateSetupSlot(index, 'pairingPort', event.target.value)} placeholder="Pair port" inputMode="numeric" disabled={processingActive} /></label>
                  <label>Covered area<select value={slot.quarter} onChange={(event) => updateSetupSlot(index, 'quarter', event.target.value)} disabled={processingActive}><option value="">Choose area</option>{QUARTER_OPTIONS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}</select></label>
                  <button className="secondary-button setup-check-button" type="button" onClick={() => checkSetupDetails(index)} disabled={processingActive || slot.checkStatus === 'checking'}>{slot.checkStatus === 'checking' ? 'Checking...' : 'Check connection'}</button>
                </div>
                <p className={`setup-message ${slot.checkStatus === 'details_valid' ? 'valid' : ''}`}>{slot.checkMessage}</p>
              </article>
            ))}
          </div>

          <div className="setup-footer">
            <button className="primary-button" type="button" onClick={() => setPage('generator')} disabled={!canProceed || processingActive}>{testMode && !setupComplete ? 'Continue in test mode' : 'Continue to QR generation'}</button>
          </div>
        </section>
          ) : page === 'generator' ? (
        <>
          <section className="generator-card" aria-label="QR code generator">
            <p className="eyebrow">QR CODES</p>
            <h2>Generate site codes</h2>
            <form className="generator-form" onSubmit={generateSite}>
              <label className="form-field" htmlFor="site-code">Site number or code<input id="site-code" name="siteCode" value={siteCode} onChange={(event) => setSiteCode(event.target.value.toUpperCase())} placeholder="MUSCAT" autoComplete="off" required /></label>
              <label className="form-field" htmlFor="cow-count">Number of cows<input id="cow-count" name="cowCount" type="number" min="1" max="10000" value={cowCount} onChange={(event) => setCowCount(event.target.value)} required /></label>
              <button className="primary-button" type="submit" disabled={generatorBusy}>{generatorBusy ? 'Generating...' : 'Generate and save'}</button>
            </form>
          </section>

          <section className="site-list-card" aria-label="Existing sites">
            <div className="setup-list-heading">
              <div><p className="eyebrow">SITES</p><h2>Existing sites</h2></div>
            </div>
            {sites.length === 0 ? <p className="section-copy">No sites</p> : <div className="site-list">
              {sites.map((site) => <div className="site-row" key={site.code}>
                <div><strong>{site.code}</strong><span>{site.cow_count} QR codes</span></div>
                <button className="remove-button" type="button" onClick={() => deleteSite(site.code)} disabled={Boolean(deletingSite) || processingActive} aria-label={`Delete site ${site.code}`}>
                  {deletingSite === site.code ? 'Deleting...' : 'Delete site'}
                </button>
              </div>)}
            </div>}
          </section>
        </>
      ) : (
        <>
          <div className="processing-controls processing-toolbar" aria-label="Processing controls">
            <label htmlFor="processing-site">Registered site<select id="processing-site" aria-label="Processing site" value={selectedSite} onChange={(event) => setSelectedSite(event.target.value)} disabled={Boolean(processing.session)}>
              <option value="">Select a site</option>
              {sites.map((site) => <option key={site.code} value={site.code}>{site.code}</option>)}
            </select></label>
            {!processing.session ? <button className="primary-button session-action-button" type="button" onClick={startSession} disabled={!selectedSite || sessionBusy}>{sessionBusy ? 'Starting...' : 'Start processing'}</button> : <div className="session-actions">
              <button className="secondary-button session-action-button" type="button" onClick={togglePauseSession} disabled={sessionBusy}>{sessionBusy ? (processingPaused ? 'Resuming...' : 'Pausing...') : (processingPaused ? 'Resume session' : 'Pause session')}</button>
              <button className="secondary-button session-action-button stop-action-button" type="button" onClick={stopSession} disabled={sessionBusy}>{sessionBusy ? 'Stopping...' : 'Stop session'}</button>
            </div>}
          </div>

          <div className="processing-workspace">
            <section className="top-camera-card processing-live-card" aria-label="Top camera live view">
              <div className="preview-header">
                <div>
                  <p className="eyebrow">OVERHEAD CAMERA</p>
                  <h2>{topCamera.name || 'Configured overhead camera'}</h2>
                </div>
                <StatusPill good={topCamera.connected}>{topCamera.connected ? 'Live' : 'Stopped'}</StatusPill>
              </div>
              <div className="top-camera-frame feed-frame">
                {processing.session && topCamera.streamActive && topCamera.hasFrame ? <img src="/api/top-camera/mjpeg" alt={`Live preview from ${topCamera.name || 'the configured overhead camera'}`} /> : <div className="empty-preview"><span>O</span><p>{processing.session ? 'Waiting for feed' : 'Feed inactive'}</p></div>}
                {eventNeedsAction && <div className="feed-warning-overlay" role="alert"><strong>Duplicate QR</strong><span>Wait for exit</span><button className="feed-warning-action" type="button" onClick={overrideVisit} disabled={sessionBusy}>Override</button></div>}
                {multipleQrEvent && <div className="feed-warning-overlay multiple-qr-warning" role="alert"><strong>Only one QR at a time</strong><span>Remove one QR from the camera view</span></div>}
                {wrongSiteEvent && <div className="feed-warning-overlay wrong-site-warning" role="alert"><strong>Wrong site QR</strong><span>{processing.session.siteCode} is active. Stop and switch site.</span><button className="feed-warning-action" type="button" onClick={stopSession} disabled={sessionBusy}>Stop session</button></div>}
                {captureInProgress && <div className="capture-progress-overlay" role="status" aria-live="polite"><span className="capture-progress-spinner" aria-hidden="true" /><strong>Capturing thermal images</strong></div>}
              </div>
              <div className="feed-meta"><span>{processing.session ? topCamera.transport : 'Feed stopped until session start'}</span><span>{processing.session && topCamera.frameCount ? `${topCamera.frameCount.toLocaleString()} frames received` : 'No active feed'}</span></div>
            </section>

            <section className="capture-gallery-card" aria-label="Recent thermal captures">
              <div className="preview-header">
                <div><p className="eyebrow">THERMAL CAPTURES</p><h2>{visibleCapture ? 'Latest synchronized images' : 'No recent capture'}</h2></div>
                {visibleCapture && <StatusPill good>{Math.ceil(Math.max(0, 10 - (Date.now() - Date.parse(visibleCapture.completedAt || Date.now())) / 1000))}s</StatusPill>}
              </div>
              <div className={`capture-gallery ${testMode ? '' : 'normal-capture-gallery'}`}>
                {visibleCapture ? visibleCapture.results.filter((result) => result.status === 'completed').map((result) => <figure className="capture-tile" key={`${visibleCapture.batchId}-${result.slot}`}><img src={result.imageUrl} alt={`Thermal capture for ${result.quarter || `camera ${result.slot}`}`} /><figcaption><span>{result.quarter || `Camera ${result.slot}`}</span><small>{result.filename}</small></figcaption></figure>) : <div className="gallery-empty"><span>O</span></div>}
              </div>
            </section>
          </div>

          <section className="camera-controls-card" aria-label="Overhead camera controls">
            <div className="camera-controls-heading">
              <div><p className="eyebrow">CAMERA CONTROLS</p><h2>Focus and exposure</h2></div>
              {!processingActive && <span className="file-meta">Start processing to adjust</span>}
            </div>
            <div className="camera-controls-grid">
              <label className="camera-slider">
                <span><strong>Exposure</strong><output>{cameraControls.exposureIndex > 0 ? '+' : ''}{cameraControls.exposureIndex}</output></span>
                <input type="range" min={cameraControls.exposureMin} max={cameraControls.exposureMax} step="1" value={cameraControls.exposureIndex} onChange={updateExposure} disabled={!processingActive || !topCamera.connected || !cameraControls.ready || !cameraControls.exposureSupported} />
              </label>
              <label className="camera-slider">
                <span><strong>Focus</strong><output>{cameraControls.focusMode === 'auto' ? 'Auto' : `${Math.round(cameraControls.focusPosition * 100)}%`}</output></span>
                <input type="range" min="0" max="100" step="1" value={Math.round(cameraControls.focusPosition * 100)} onChange={updateFocus} disabled={!processingActive || !topCamera.connected || !cameraControls.ready || !cameraControls.focusSupported} />
              </label>
              <button className="secondary-button focus-reset-button" type="button" onClick={resetFocus} disabled={!processingActive || !topCamera.connected || !cameraControls.ready || !cameraControls.focusSupported}>Auto focus</button>
            </div>
            {processingActive && cameraControls.ready && (!cameraControls.focusSupported || !cameraControls.exposureSupported) && <p className="camera-control-note">{!cameraControls.exposureSupported ? 'Exposure control is not available on this phone.' : 'Manual focus is not available on this phone.'}</p>}
          </section>

          <section className="visits-card" aria-label="Processing visits">
            <div className="preview-header"><div><p className="eyebrow">CURRENT SESSION</p><h2>Registered cows</h2></div><span className="file-meta">{processing.visits.length} records</span></div>
            {processing.visits.length === 0 ? <p className="section-copy">No visits</p> : <div className="visit-list">{processing.visits.map((visit) => <div className="visit-row" key={visit.id}><span>{visit.site_code} / {visit.cow_number.toString().padStart(3, '0')}</span><StatusPill good={visit.capture_status === 'completed'} failed={visit.capture_status === 'failed'}>{visit.capture_status || 'pending'}</StatusPill></div>)}</div>}
          </section>

        </>
      )}

        </div>
      </div>
    </main>
  );
}
