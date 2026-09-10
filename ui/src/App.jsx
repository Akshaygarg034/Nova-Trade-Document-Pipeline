import { useCallback, useEffect, useRef, useState } from 'react'

const STAGES = ['ingest', 'extract', 'validate', 'route', 'completed']

const VERDICT_LABEL = { found: 'verified', uncertain: 'uncertain', not_found: 'absent' }
const STATUS_LABEL = {
  match: 'match',
  mismatch: 'mismatch',
  uncertain: 'uncertain',
  not_applicable: 'n/a',
}
const DECISION_LABEL = {
  auto_approve: 'Auto-approved',
  flag_for_review: 'Needs human review',
  amendment_request: 'Amendment required',
}

// Flags are machine-readable in the database; an operator needs the plain
// meaning, so each one is spelled out rather than dumped as a slug.
const FLAG_TEXT = {
  reported_absent: 'The model reported this field as not present on the document.',
  possible_hallucination: 'The value could not be found anywhere in the document text.',
  value_absent_from_document: 'This value does not appear in the document text.',
  weak_grounding: 'The value was only partially matched against the document.',
  no_text_corpus: 'No readable text layer, so the value could not be independently checked.',
  no_evidence_quote: 'The model gave no supporting quote.',
  quote_does_not_contain_value: 'The supporting quote does not contain the value.',
  evidence_citation_not_verbatim: 'The supporting quote is not word-for-word from the page.',
  code_not_present_as_whole_word: 'This code does not appear as a standalone word on the page.',
  partial_token_match: 'Only part of a reference number on the page was captured.',
  token_alignment_unverified: 'Scan quality prevented an exact reference-number check.',
  identifier_repaired: 'A field label was stripped from the value automatically.',
  format_invalid: 'The value does not satisfy the required format.',
  unverifiable_no_corpus: 'No text was available to verify this value against.',
  confirmed_by_reextraction: 'A second read of the page agreed with the first.',
  reextraction_disagreement: 'Two reads of the page disagreed, so this needs a human.',
  unverified_no_corpus: 'No text was available to verify this value against.',
}

function flagText(flag) {
  if (flag.startsWith('snapped_to_source:')) {
    return `The model read "${flag.split(':').slice(1).join(':')}"; corrected to the text printed on the page.`
  }
  if (flag.startsWith('digits_not_on_page:')) {
    return `These digits do not appear on the page: ${flag.split(':').slice(1).join(':')}`
  }
  return FLAG_TEXT[flag] || flag
}

function confidenceBand(c) {
  if (c >= 0.85) return 'high'
  if (c >= 0.6) return 'mid'
  return 'low'
}

function api(path, opts) {
  return fetch(path, opts).then(async (r) => {
    if (!r.ok) throw new Error((await r.text()) || r.statusText)
    return r.json()
  })
}

/* ------------------------------------------------------------------ */

function StageBar({ stage, status }) {
  const idx = STAGES.indexOf(stage)
  return (
    <ol className="stages">
      {STAGES.map((s, i) => {
        const state =
          status === 'failed' && i === idx + 1 ? 'failed'
            : i <= idx ? 'done'
            : i === idx + 1 ? 'active'
            : 'todo'
        return (
          <li key={s} className={`stage ${state}`}>
            <span className="dot" />
            {s}
          </li>
        )
      })}
    </ol>
  )
}

function FieldRow({ f, docId, expanded, onToggle }) {
  const conf = f.final_confidence ?? 0
  const band = confidenceBand(conf)
  const status = f.status || (f.verdict === 'not_found' ? 'not_applicable' : '')
  const isProblem = status === 'mismatch' || status === 'uncertain'

  return (
    <>
      <tr className={`field ${isProblem ? 'problem' : ''}`} onClick={onToggle}>
        <td className="chev">{expanded ? '▾' : '▸'}</td>
        <td className="label">{f.label || f.name}</td>
        <td className="value">
          {f.value || <span className="absent">not present</span>}
        </td>
        <td className="expected">{f.expected || ''}</td>
        <td className="conf">
          <div className="meter">
            <span className={`fill ${band}`} style={{ width: `${Math.round(conf * 100)}%` }} />
          </div>
          <span className={`num ${band}`}>{conf.toFixed(2)}</span>
        </td>
        <td>
          <span className={`pill ${status}`}>{STATUS_LABEL[status] || VERDICT_LABEL[f.verdict]}</span>
          {f.severity === 'blocking' && status === 'mismatch' && (
            <span className="pill blocking">blocking</span>
          )}
        </td>
      </tr>
      {expanded && (
        <tr className="detail">
          <td />
          <td colSpan={5}>
            <div className="detail-grid">
              <div>
                <h4>What was found vs expected</h4>
                <dl>
                  <dt>Found</dt>
                  <dd>{f.value || <em>not present on the document</em>}</dd>
                  <dt>Expected</dt>
                  <dd>{f.expected || <em>no rule configured</em>}</dd>
                  <dt>Result</dt>
                  <dd>{f.message}</dd>
                  {f.why && (<><dt>Why it matters</dt><dd className="why">{f.why}</dd></>)}
                </dl>
              </div>
              <div>
                <h4>Evidence from the document</h4>
                {f.evidence_quote ? (
                  <blockquote>
                    &ldquo;{f.evidence_quote}&rdquo;
                    <cite>page {f.evidence_page ?? 1}</cite>
                  </blockquote>
                ) : (
                  <p className="muted">No supporting quote was returned.</p>
                )}
                <dl className="scores">
                  <dt>Model self-report</dt><dd>{(f.model_confidence ?? 0).toFixed(2)}</dd>
                  <dt>Grounding score</dt><dd>{(f.grounding_score ?? 0).toFixed(2)}</dd>
                  <dt>Verified on page</dt><dd>{f.grounded ? 'yes' : 'no'}</dd>
                </dl>
                {f.flags?.length > 0 && (
                  <ul className="flags">
                    {f.flags.map((fl) => <li key={fl}>{flagText(fl)}</li>)}
                  </ul>
                )}
                {docId && (
                  <a
                    className="page-link"
                    href={`/api/documents/${docId}/page?n=${f.evidence_page ?? 1}`}
                    target="_blank"
                    rel="noreferrer"
                  >
                    Open page image
                  </a>
                )}
              </div>
            </div>
          </td>
        </tr>
      )}
    </>
  )
}

function DraftPanel({ run, onSent }) {
  const d = run.decision
  const [subject, setSubject] = useState(d?.draft_subject || '')
  const [body, setBody] = useState(d?.draft_body || '')
  const [sending, setSending] = useState(false)
  const [sent, setSent] = useState(!!d?.email_sent)

  useEffect(() => {
    setSubject(d?.draft_subject || '')
    setBody(d?.draft_body || '')
    setSent(!!d?.email_sent)
  }, [d?.run_id, d?.draft_subject, d?.draft_body, d?.email_sent])

  if (!d?.draft_body) return null

  async function send() {
    setSending(true)
    try {
      await api(`/api/runs/${run.run_id}/send`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ subject, body }),
      })
      setSent(true)
      onSent?.()
    } finally {
      setSending(false)
    }
  }

  return (
    <section className="card draft">
      <header>
        <h3>Draft reply to supplier</h3>
        <span className="muted small">
          generated by {d.draft_source === 'template' ? 'fallback template' : 'agent'} · editable
        </span>
      </header>
      <label>Subject</label>
      <input value={subject} onChange={(e) => setSubject(e.target.value)} />
      <label>Message</label>
      <textarea rows={14} value={body} onChange={(e) => setBody(e.target.value)} />
      <div className="draft-actions">
        <button className="primary" onClick={send} disabled={sending || sent}>
          {sent ? 'Sent by operator' : sending ? 'Recording…' : 'Send to supplier'}
        </button>
        <p className="muted small">
          The agent never sends. Nothing leaves this system until you click send.
        </p>
      </div>
    </section>
  )
}

function AskPanel() {
  const [q, setQ] = useState('How many shipments were flagged for review this week?')
  const [res, setRes] = useState(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)

  const SUGGESTIONS = [
    'How many shipments were flagged for review this week?',
    'Which field fails validation most often?',
    'Show me every field that could not be read reliably.',
    'How much have we spent on LLM calls in total?',
  ]

  async function run(question) {
    setBusy(true); setErr(null); setRes(null)
    try {
      setRes(await api('/api/ask', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question }),
      }))
    } catch (e) {
      setErr(String(e.message || e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="card">
      <header><h3>Ask about stored results</h3></header>
      <div className="ask-row">
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && run(q)}
          placeholder="Ask a question in plain English"
        />
        <button className="primary" onClick={() => run(q)} disabled={busy}>
          {busy ? 'Asking…' : 'Ask'}
        </button>
      </div>
      <div className="suggestions">
        {SUGGESTIONS.map((s) => (
          <button key={s} className="chip" onClick={() => { setQ(s); run(s) }}>{s}</button>
        ))}
      </div>
      {err && <p className="error">{err}</p>}
      {res && (
        <div className="answer">
          <p className="answer-text">{res.answer}</p>
          {res.warnings?.length > 0 && (
            <ul className="flags">{res.warnings.map((w, i) => <li key={i}>{w}</li>)}</ul>
          )}
          <details>
            <summary>Show the query and rows ({res.row_count})</summary>
            <pre className="sql">{res.sql}</pre>
            {res.rows?.length > 0 && (
              <table className="rows">
                <thead><tr>{res.columns.map((c) => <th key={c}>{c}</th>)}</tr></thead>
                <tbody>
                  {res.rows.slice(0, 20).map((r, i) => (
                    <tr key={i}>{r.map((v, j) => <td key={j}>{v === null ? '' : String(v)}</td>)}</tr>
                  ))}
                </tbody>
              </table>
            )}
          </details>
        </div>
      )}
    </section>
  )
}

/* ------------------------------------------------------------------ */

export default function App() {
  const [runs, setRuns] = useState([])
  const [run, setRun] = useState(null)
  const [progress, setProgress] = useState(null)
  const [expanded, setExpanded] = useState(null)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState(null)
  const esRef = useRef(null)

  const refreshRuns = useCallback(() => {
    api('/api/runs').then(setRuns).catch(() => {})
  }, [])

  useEffect(() => { refreshRuns() }, [refreshRuns])

  const openRun = useCallback((runId) => {
    setExpanded(null)
    api(`/api/runs/${runId}`).then(setRun).catch((e) => setError(String(e.message || e)))
  }, [])

  const follow = useCallback((runId) => {
    esRef.current?.close()
    const es = new EventSource(`/api/runs/${runId}/stream`)
    esRef.current = es
    es.onmessage = (ev) => {
      const data = JSON.parse(ev.data)
      setProgress(data)
      if (data.stage) openRun(runId)
      if (data.done) {
        es.close()
        openRun(runId)
        refreshRuns()
        setProgress(null)
      }
    }
    es.onerror = () => { es.close(); setProgress(null) }
  }, [openRun, refreshRuns])

  useEffect(() => () => esRef.current?.close(), [])

  async function upload(file) {
    if (!file) return
    setUploading(true); setError(null); setRun(null)
    try {
      const fd = new FormData()
      fd.append('file', file)
      const { run_id } = await api('/api/runs', { method: 'POST', body: fd })
      setProgress({ stage: 'ingest', status: 'running' })
      follow(run_id)
      refreshRuns()
    } catch (e) {
      setError(String(e.message || e))
    } finally {
      setUploading(false)
    }
  }

  const d = run?.decision
  const problems = run?.fields?.filter((f) => f.status === 'mismatch' || f.status === 'uncertain') || []

  return (
    <div className="app">
      <header className="topbar">
        <div>
          <h1>Nova <span className="thin">· Trade Document Pipeline</span></h1>
          <p className="muted small">Extract → validate → decide, with evidence for every field.</p>
        </div>
        <label className={`upload ${uploading ? 'busy' : ''}`}>
          {uploading ? 'Uploading…' : 'Upload a document'}
          <input
            type="file"
            accept=".pdf,.png,.jpg,.jpeg,.tif,.tiff,.webp,.bmp"
            onChange={(e) => upload(e.target.files?.[0])}
          />
        </label>
      </header>

      {error && <p className="error banner">{error}</p>}

      <div className="layout">
        <aside className="sidebar">
          <h3>Recent runs</h3>
          {runs.length === 0 && <p className="muted small">Nothing yet. Upload a document.</p>}
          <ul className="runlist">
            {runs.map((r) => (
              <li
                key={r.run_id}
                className={run?.run_id === r.run_id ? 'sel' : ''}
                onClick={() => openRun(r.run_id)}
              >
                <span className="fname">{r.filename || r.doc_id}</span>
                <span className={`pill ${r.decision || r.status}`}>
                  {DECISION_LABEL[r.decision] || r.status}
                </span>
                <span className="muted small">${(r.usd_cost || 0).toFixed(4)}</span>
              </li>
            ))}
          </ul>
        </aside>

        <main className="main">
          {(progress || run) && (
            <section className="card">
              <StageBar
                stage={progress?.stage || run?.stage || 'ingest'}
                status={progress?.status || run?.status}
              />
              {run && (
                <p className="muted small runmeta">
                  {run.run_id} · shipment {run.shipment_id} · {run.latency_ms} ms ·
                  ${(run.usd_cost || 0).toFixed(5)} · {run.spans?.length || 0} LLM calls
                </p>
              )}
              {run?.error && <p className="error">{run.error}</p>}
            </section>
          )}

          {run && d && (
            <section className={`card decision ${d.decision}`}>
              <header>
                <h2>{DECISION_LABEL[d.decision]}</h2>
                <span className="muted small">
                  {d.n_mismatch} mismatch · {d.n_uncertain} uncertain · lowest confidence{' '}
                  {(d.lowest_confidence ?? 0).toFixed(2)}
                </span>
              </header>
              <p className="rationale">{d.rationale}</p>
              <details>
                <summary>Why the system decided this ({d.policy_reasons?.length || 0} rules)</summary>
                <ul className="reasons">
                  {d.policy_reasons?.map((r, i) => <li key={i}>{r}</li>)}
                </ul>
              </details>
              {d.warnings?.length > 0 && (
                <ul className="flags">{d.warnings.map((w, i) => <li key={i}>{w}</li>)}</ul>
              )}
            </section>
          )}

          {run?.fields?.length > 0 && (
            <section className="card">
              <header>
                <h3>Fields</h3>
                <span className="muted small">
                  {problems.length > 0
                    ? `${problems.length} need attention — click any row for evidence`
                    : 'all fields verified — click any row for evidence'}
                </span>
              </header>
              <table className="fields">
                <thead>
                  <tr>
                    <th /><th>Field</th><th>Found on document</th>
                    <th>Required</th><th>Confidence</th><th>Result</th>
                  </tr>
                </thead>
                <tbody>
                  {run.fields.map((f) => (
                    <FieldRow
                      key={f.name}
                      f={f}
                      docId={run.doc_id}
                      expanded={expanded === f.name}
                      onToggle={() => setExpanded(expanded === f.name ? null : f.name)}
                    />
                  ))}
                </tbody>
              </table>
            </section>
          )}

          {run && <DraftPanel run={run} onSent={refreshRuns} />}

          <AskPanel />
        </main>
      </div>
    </div>
  )
}
