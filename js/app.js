/* ═══════════════════════════════════════════════════════════
   Trainer CV Builder — app.js
   Live form → CV preview binding + dynamic list management
   ═══════════════════════════════════════════════════════════ */

'use strict';

/* ── Default sample data ─────────────────────────────────── */
const DEFAULTS = {
  trainerName:  'Samir F.',
  csatScore:    '4.9',
  batches:      '30',
  tagline:      'IT Governance Expert | ITIL® 4 Trainer | COBIT® 2019 Consultant | ISO 27001 & Information Security Specialist | Digital Transformation Advisor',
  bio1:         'Samir is a highly accomplished IT governance and service management expert with over 20 years of international experience across government, banking, finance, and enterprise sectors. He is widely recognized for his deep expertise in ITIL® 4, COBIT® 2019, and ISO standards, particularly ISO 27001, where he has helped organizations strengthen governance, enhance security posture, and achieve compliance excellence. Throughout his career, Samir has led large-scale IT governance transformations and service management initiatives, enabling organizations to align IT services with business strategy while improving operational efficiency and reducing risk.',
  bio2:         'His work spans ministries, municipalities, national authorities, and global enterprises, where he has successfully implemented governance frameworks that elevate organizational maturity. As a trainer, Samir brings a practical, engaging, and results-driven approach. He combines real-world case studies with certification-focused guidance, helping professionals not only pass exams but confidently apply ITIL, COBIT, and ISO 27001 practices in their organizations. His passion for knowledge sharing is also reflected in his widely adopted online courses and workshops, where he continues to empower IT professionals globally.',
  programs: [
    'Corporate Finance',
    'Emotional Intelligence',
    'Islamic Banking and Finance',
    'Leadership and Team Building',
    'Leadership & Decision-Making Skills',
    'Finance for Non-Finance Professionals',
    'Leadership & Management for Engineers',
    'Executive Communication, Leadership & Presentation Skills',
    'Emotional Intelligence & Empathy-Driven Leadership',
  ],
  training: [
    'Amazon – Egypt',
    'Bukhatir',
    'ZAJEL',
    'Astro Offshore',
    'Proof point UAE',
    'Al Fahad Electrical Systems',
    'Dubai Chambers',
    'Servier Pharma',
    'Healthcare Pharmaceuticals',
    'Global Esoft',
    'United Education',
    'Hapag Lloyd - United Arab Shipping Company Limited',
    'AC Business Experts',
    'KIA Middle East and Africa FZE',
    'Creative Vision General Trading LLC',
    'EMKA Beschlagteile Middle East FZE',
  ],
  strengths: [
    'Emotional Intelligence',
    'Communication Skills',
    'Presentation Skills',
    'Sales',
    'Finance',
    'Recruitment',
    'Leadership and Management',
    'Educational Consulting',
    'Sustainability',
    'Corporate Sustainability',
    'Training and Development',
    'Motivational Speaking',
    'Strategic Thinking',
    'Negotiation Skills',
    'Personal Development',
  ],
  experience: [
    'CEO and Co-Founder – Consulting Director',
    'Director of Student Affairs — International Open University',
    'Faculty Manager — International Open University',
    'Senior Lecturer, Business Administration & Islamic Banking & Economics Department — International Open University',
    'Head of Corporate Relationship Program — International Open University',
    'Acting Head of Department, Business Administration — International Open University',
    'Consultant & Advisor — Endeavour International School',
    'Consultant & Advisor — Iqra International School',
    'Head of Sales / Student Recruitment Team — International Open University',
    'Business Associate and Franchise Owner — Iadaf Investments Private Limited',
    'Senior Associate, Financial Planning (Wealth Management) — HSBC',
    'Assistant Sales Manager, Personal Financial Services — HSBC',
  ],
  awards: [
    'Chaired a session at the 2nd IOCRIS, International Open University Conference on Research and Integrated Sciences, 2022',
    'Invited for pre-release book review and proof-reading by Bloomsbury Academic in 2020 for their publication \'Islamic Business Administration – Concepts and Strategies\'',
    'Attended the ICIFE, International Council for Islamic Finance Educators annual conference in Malaysia, 2019',
    'Best Manager – Indian Institute of Social Welfare and Business Management, Feb 2013',
  ],
};

/* ── State ───────────────────────────────────────────────── */
const state = { zoom: 1.0 };
const listManagers = {};

/* ── DOM helpers ─────────────────────────────────────────── */
const $ = (sel, ctx = document) => ctx.querySelector(sel);
const $$ = (sel, ctx = document) => ctx.querySelectorAll(sel);

/* ── Simple text binding ─────────────────────────────────── */
function bindTextField(inputId, ...cvSelectors) {
  const input = document.getElementById(inputId);
  if (!input) return;
  const update = () => {
    const val = input.value;
    cvSelectors.forEach(sel => {
      $$(sel).forEach(el => { el.textContent = val; });
    });
  };
  input.addEventListener('input', update);
  update();
}

/* ── Dynamic list ─────────────────────────────────────────── */
function createListManager(config) {
  const { formContainerId, cvSelectors, defaults } = config;
  const container = document.getElementById(formContainerId);
  if (!container) return;

  const itemsWrap = container.querySelector('.dynamic-list-items');
  const addBtn    = container.querySelector('.btn-add-item');

  /* Render all CV targets from current items */
  function renderCV() {
    const inputs = itemsWrap.querySelectorAll('input');
    const items  = Array.from(inputs).map(i => i.value.trim()).filter(Boolean);

    cvSelectors.forEach(sel => {
      $$(sel).forEach(ul => {
        ul.innerHTML = items.map(item =>
          `<li>${escapeHtml(item)}</li>`
        ).join('');
      });
    });
  }

  /* Create a single list-item row */
  function createRow(value = '') {
    const row = document.createElement('div');
    row.className = 'dynamic-list-item';
    row.innerHTML = `
      <span class="drag-handle" title="Drag to reorder">⠿</span>
      <input type="text" placeholder="Add item…" value="${escapeHtml(value)}">
      <button class="btn-remove-item" title="Remove">×</button>
    `;

    row.querySelector('input').addEventListener('input', renderCV);
    row.querySelector('.btn-remove-item').addEventListener('click', () => {
      row.remove();
      renderCV();
    });

    /* Drag-and-drop reorder */
    setupDrag(row, itemsWrap, renderCV);
    return row;
  }

  function setItems(values) {
    itemsWrap.innerHTML = '';
    values.forEach(val => itemsWrap.appendChild(createRow(val)));
    renderCV();
  }

  /* Populate from defaults */
  setItems(defaults);

  /* Add-item button */
  addBtn.addEventListener('click', () => {
    const row = createRow('');
    itemsWrap.appendChild(row);
    row.querySelector('input').focus();
    renderCV();
  });

  return { renderCV, setItems };
}

/* ── Drag-to-reorder ─────────────────────────────────────── */
function setupDrag(row, container, onDrop) {
  const handle = row.querySelector('.drag-handle');
  if (!handle) return;

  handle.addEventListener('mousedown', e => {
    e.preventDefault();
    const rows     = Array.from(container.querySelectorAll('.dynamic-list-item'));
    const idx      = rows.indexOf(row);
    const startY   = e.clientY;
    const rowH     = row.offsetHeight + 5; // 5px gap
    let currentIdx = idx;

    row.style.opacity  = '0.5';
    row.style.position = 'relative';
    row.style.zIndex   = '50';

    const onMove = mv => {
      const delta  = mv.clientY - startY;
      const newIdx = Math.max(0, Math.min(rows.length - 1,
                     idx + Math.round(delta / rowH)));
      if (newIdx !== currentIdx) {
        currentIdx = newIdx;
        const sibling = container.querySelectorAll('.dynamic-list-item')[newIdx];
        if (sibling) {
          const after = currentIdx > idx;
          container.insertBefore(row, after ? sibling.nextSibling : sibling);
        }
      }
    };

    const onUp = () => {
      row.style.opacity  = '';
      row.style.position = '';
      row.style.zIndex   = '';
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup',   onUp);
      onDrop();
    };

    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup',   onUp);
  });
}

/* ── Collapsible sections ────────────────────────────────── */
function initCollapsibles() {
  $$('.form-section-header').forEach(header => {
    header.addEventListener('click', () => {
      header.closest('.form-section').classList.toggle('collapsed');
    });
  });
}

/* ── Preview zoom ────────────────────────────────────────── */
function initZoom() {
  const scaler   = document.querySelector('.cv-preview-scaler');
  const zoomLbl  = document.querySelector('.preview-zoom-label');
  const scrollEl = document.querySelector('.preview-scroll-area');
  let naturalH   = 0;

  function measureNaturalHeight() {
    /* Temporarily remove transform to measure true layout height */
    const prev = scaler.style.transform;
    scaler.style.transform = 'none';
    scaler.style.marginBottom = '0';
    naturalH = scaler.getBoundingClientRect().height;
    scaler.style.transform = prev;
  }

  function applyZoom() {
    if (!scaler) return;
    const z = state.zoom;
    scaler.style.transform = `scale(${z})`;
    scaler.style.transformOrigin = 'top center';

    /* Compensate layout height: transform:scale doesn't affect flow.
       margin-bottom = naturalH * (z - 1) pulls up (z<1) or expands (z>1) */
    if (naturalH > 0) {
      scaler.style.marginBottom = `${naturalH * (z - 1)}px`;
    }

    if (zoomLbl) zoomLbl.textContent = Math.round(z * 100) + '%';
  }

  function fitToPanel() {
    if (!scrollEl || !scaler) return;
    measureNaturalHeight();
    const panelW   = scrollEl.clientWidth - 56; /* 28px padding each side */
    const computed = Math.min(1.5, Math.max(0.3, panelW / 595));
    state.zoom     = Math.round(computed * 10) / 10;
    applyZoom();
  }

  const btnIn  = document.getElementById('btn-zoom-in');
  const btnOut = document.getElementById('btn-zoom-out');
  const btnFit = document.getElementById('btn-zoom-fit');

  if (btnIn)  btnIn.addEventListener('click', () => {
    measureNaturalHeight();
    state.zoom = Math.min(2, +(state.zoom + 0.1).toFixed(1));
    applyZoom();
  });
  if (btnOut) btnOut.addEventListener('click', () => {
    measureNaturalHeight();
    state.zoom = Math.max(0.2, +(state.zoom - 0.1).toFixed(1));
    applyZoom();
  });
  if (btnFit) btnFit.addEventListener('click', fitToPanel);

  /* Initial fit — wait one frame for layout to settle */
  requestAnimationFrame(() => requestAnimationFrame(fitToPanel));
  window.addEventListener('resize', fitToPanel);
}

/* ── Print ───────────────────────────────────────────────── */
function initPrint() {
  const btn = document.getElementById('btn-print');
  if (btn) btn.addEventListener('click', () => window.print());
}

/* ── Reset to defaults ───────────────────────────────────── */
function initReset() {
  const btn = document.getElementById('btn-reset');
  if (!btn) return;
  btn.addEventListener('click', () => {
    if (!confirm('Reset all fields to the sample data?')) return;
    location.reload();
  });
}

/* ── Escape HTML ─────────────────────────────────────────── */
function escapeHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function setInputValue(inputId, value) {
  const el = document.getElementById(inputId);
  if (!el) return;
  el.value = value || '';
  el.dispatchEvent(new Event('input'));
}

function setStatus(message, isError = false) {
  const statusEl = document.getElementById('generation-status');
  if (!statusEl) return;
  statusEl.textContent = message;
  statusEl.style.color = isError ? '#dc2626' : '#374151';
}

function parseMultilinePaths(value) {
  return String(value || '')
    .split('\n')
    .map(v => v.trim())
    .filter(Boolean);
}

function applyGeneratedProfile(profile) {
  const safeProfile = profile || {};
  const titles = Array.isArray(safeProfile.professional_titles)
    ? safeProfile.professional_titles.filter(Boolean)
    : [];
  const profileText = safeProfile.profile || '';

  const guessedName = guessTrainerNameFromProfileText(profileText);
  if (guessedName) {
    setInputValue('f-name', guessedName);
  }

  setInputValue('f-tagline', titles.join(' | '));

  const paraSplit = profileText
    .split(/\n{2,}/)
    .map(v => v.trim())
    .filter(Boolean);
  setInputValue('f-bio1', paraSplit[0] || profileText);
  setInputValue('f-bio2', paraSplit.length > 1 ? paraSplit.slice(1).join('\n\n') : '');

  if (listManagers.programs) {
    listManagers.programs.setItems(safeProfile.programs_trained || []);
  }
  if (listManagers.training) {
    const trainingItems = Array.isArray(safeProfile.training_delivered) && safeProfile.training_delivered.length
      ? safeProfile.training_delivered
      : [
          ...(safeProfile.education || []),
          ...(safeProfile.certificates || []),
          ...(safeProfile.board_experience || []),
        ];
    listManagers.training.setItems(trainingItems);
  }
  if (listManagers.strengths) {
    const strengths = Array.isArray(safeProfile.key_skills) && safeProfile.key_skills.length
      ? safeProfile.key_skills
      : (safeProfile.core_competencies || []);
    listManagers.strengths.setItems(strengths);
  }
  if (listManagers.experience) {
    listManagers.experience.setItems(safeProfile.professional_experience || []);
  }
  if (listManagers.awards) {
    const awards = [
      ...(safeProfile.awards_and_recognitions || []),
      ...(safeProfile.education || []),
      ...(safeProfile.certificates || []),
      ...(safeProfile.board_experience || []),
    ];
    listManagers.awards.setItems(awards);
  }
}

function getQueryParam(name) {
  const params = new URLSearchParams(window.location.search);
  return params.get(name);
}

function resolveApiBase() {
  const fromQuery = (getQueryParam('api_base') || '').trim();
  if (fromQuery) return fromQuery.replace(/\/$/, '');

  const input = document.getElementById('api-base-url');
  const fromInput = (input?.value || '').trim();
  if (fromInput) return fromInput.replace(/\/$/, '');

  return window.location.origin.replace(/\/$/, '');
}

function guessTrainerNameFromProfileText(text) {
  const t = String(text || '').trim();
  if (!t) return '';
  // Common pattern: "First Last is a ..." / "First Middle Last is ..."
  const m = t.match(/^([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})\s+is\b/);
  return m ? m[1].trim() : '';
}

async function loadJobFromQueryIfPresent() {
  const jobId = (getQueryParam('job') || '').trim();
  if (!jobId) return;

  const apiBase = resolveApiBase();
  const apiInput = document.getElementById('api-base-url');
  if (apiInput) {
    apiInput.value = apiBase;
  }

  setStatus(`Loading job ${jobId}...`);
  try {
    const response = await fetch(`${apiBase}/api/v1/profiles/${encodeURIComponent(jobId)}`);
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data?.detail || 'Failed to load job.');
    }
    if (data.status !== 'completed' || !data.generated_profile) {
      throw new Error('Job is not ready or has no generated profile yet.');
    }

    applyGeneratedProfile(data.generated_profile);
    console.info('[trainer-profile] job data loaded', { jobId, status: data.status, keys: Object.keys(data) });
    setStatus(`Loaded job ${jobId}. Use Print / Export PDF to download a PDF.`);
  } catch (error) {
    console.error('[trainer-profile] job load failed', { jobId, apiBase, error });
    setStatus(error?.message || 'Failed to load job.', true);
  }

  const shouldAutoPrint = getQueryParam('autoprint') === '1';
  if (shouldAutoPrint) {
    // Give the layout a moment to reflow after large list fills
    requestAnimationFrame(() => requestAnimationFrame(() => window.print()));
  }
}

async function initAIGeneration() {
  const btn = document.getElementById('btn-generate-profile');
  if (!btn) return;

  btn.addEventListener('click', async () => {
    const baseUrl = (document.getElementById('api-base-url')?.value || '').trim();
    const zohoRecordId = (document.getElementById('f-zoho-record-id')?.value || '').trim();
    const cvZohoId = (document.getElementById('f-cv-zoho-id')?.value || '').trim();
    const cvPath = (document.getElementById('f-cv-path')?.value || '').trim();
    const outlineRaw = document.getElementById('f-outline-paths')?.value || '';
    const provider = (document.getElementById('f-provider')?.value || '').trim();
    const modelName = (document.getElementById('f-model-name')?.value || '').trim();

    if (!baseUrl || !zohoRecordId) {
      setStatus('API Base URL and Zoho Record ID are required.', true);
      return;
    }
    if (!cvZohoId && !cvPath) {
      setStatus('Provide either Zoho CV file ID or a local CV file path (not both).', true);
      return;
    }
    if (cvZohoId && cvPath) {
      setStatus('Use either Zoho CV file ID or local CV path — clear one of the two fields.', true);
      return;
    }

    const payload = {
      zoho_record_id: zohoRecordId,
      ...(cvZohoId ? { cv: cvZohoId } : { cv_path: cvPath }),
      course_outline_paths: parseMultilinePaths(outlineRaw),
      provider: provider || null,
      model_name: modelName || null,
    };

    btn.disabled = true;
    setStatus('Generating trainer profile. Please wait...');

    try {
      const response = await fetch(`${baseUrl}/api/v1/profiles/generate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data?.detail || 'Failed to generate profile.');
      }

      applyGeneratedProfile(data.generated_profile || {});
      if (data.pdf_generation_error) {
        console.warn('[trainer-profile] PDF generation error', data.pdf_generation_error);
      }
      const exportUi = data?.export?.trainer_profile_ui;
      const exportPrint = data?.export?.trainer_profile_print;
      const exportPdf = data?.export?.trainer_profile_pdf;
      const exportPdfFile = data?.export?.pdf_url || data?.pdf_url;
      if (exportUi) {
        const pdfWarn = data.pdf_generation_error
          ? `\nPDF not saved: ${data.pdf_generation_error}`
          : '';
        setStatus(
          `Profile generated. Job ID: ${data.id}\n` +
          `Open UI: ${exportUi}\n` +
          (exportPrint ? `Open + print: ${exportPrint}\n` : '') +
          (exportPdfFile ? `PDF file URL: ${exportPdfFile}\n` : '') +
          (exportPdf ? `PDF API URL: ${exportPdf}` : '') +
          pdfWarn
        );
      } else {
        setStatus(`Profile generated successfully. Job ID: ${data.id}`);
      }
    } catch (error) {
      setStatus(error?.message || 'Generation failed.', true);
    } finally {
      btn.disabled = false;
    }
  });
}

/* ── Bootstrap ───────────────────────────────────────────── */
document.addEventListener('DOMContentLoaded', () => {
  const jobId = (getQueryParam('job') || '').trim();
  if (jobId) {
    // Skip clobbering the UI with the sample "Samir" defaults; we'll load the generated profile.
    // Still initialize managers with empty lists by temporarily overriding DEFAULTS.
    // (We do this by clearing defaults via list managers after init below.)
  }

  /* ── Simple text fields */
  bindTextField('f-name',    '.cv-p1-name');
  bindTextField('f-csat',    '.cv-csat-score-val');
  bindTextField('f-batches', '.cv-batches-val');
  bindTextField('f-tagline', '.cv-p1-tagline');
  bindTextField('f-bio1',    '.cv-bio-1');
  bindTextField('f-bio2',    '.cv-bio-2');

  /* ── Dynamic lists */
  listManagers.programs = createListManager({
    formContainerId: 'list-programs',
    cvSelectors:     ['#cv-p1-programs-ul', '#cv-p2-programs-ul'],
    defaults:         DEFAULTS.programs,
  });

  listManagers.training = createListManager({
    formContainerId: 'list-training',
    cvSelectors:     ['#cv-p2-training-ul'],
    defaults:         DEFAULTS.training,
  });

  listManagers.strengths = createListManager({
    formContainerId: 'list-strengths',
    cvSelectors:     ['#cv-p2-strengths-ul'],
    defaults:         DEFAULTS.strengths,
  });

  listManagers.experience = createListManager({
    formContainerId: 'list-experience',
    cvSelectors:     ['#cv-p3-experience-ul'],
    defaults:         DEFAULTS.experience,
  });

  listManagers.awards = createListManager({
    formContainerId: 'list-awards',
    cvSelectors:     ['#cv-p3-awards-ul'],
    defaults:         DEFAULTS.awards,
  });

  /* Populate simple text inputs with defaults */
  if (!jobId) {
    const simpleDefaults = {
      'f-name':    DEFAULTS.trainerName,
      'f-csat':    DEFAULTS.csatScore,
      'f-batches': DEFAULTS.batches,
      'f-tagline': DEFAULTS.tagline,
      'f-bio1':    DEFAULTS.bio1,
      'f-bio2':    DEFAULTS.bio2,
    };
    Object.entries(simpleDefaults).forEach(([id, val]) => {
      const el = document.getElementById(id);
      if (el) { el.value = val; el.dispatchEvent(new Event('input')); }
    });
  } else {
    // Clear the sample dynamic lists; job loader will repopulate.
    listManagers.programs.setItems([]);
    listManagers.training.setItems([]);
    listManagers.strengths.setItems([]);
    listManagers.experience.setItems([]);
    listManagers.awards.setItems([]);

    const emptyText = { 'f-name': '', 'f-csat': '', 'f-batches': '', 'f-tagline': '', 'f-bio1': '', 'f-bio2': '' };
    Object.entries(emptyText).forEach(([id, val]) => {
      const el = document.getElementById(id);
      if (el) { el.value = val; el.dispatchEvent(new Event('input')); }
    });
  }

  initCollapsibles();
  initZoom();
  initPrint();
  initReset();
  initAIGeneration();
  if (jobId) {
    loadJobFromQueryIfPresent();
  }
});
