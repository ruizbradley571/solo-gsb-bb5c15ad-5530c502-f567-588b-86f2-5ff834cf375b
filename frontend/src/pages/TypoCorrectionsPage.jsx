import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api } from '../api.js'
import { fmtTime } from '../utils.js'

/** Validate the correction rows client-side; returns '' when they are sound. */
export function validatePairs(rows) {
  const seen = new Set()
  for (const row of rows) {
    const from = (row.from || '').trim()
    const to = (row.to || '').trim()
    if (!from && !to) continue
    if (!from) return '存在未填写错词的校正行，请补全或删除该行'
    if (!to) return `错词「${from}」的正词为空，请填写后再提交`
    if (seen.has(from)) return `错词「${from}」配置了多条校正，请只保留一条`
    seen.add(from)
  }
  return ''
}

export default function TypoCorrectionsPage() {
  const { id: projectId } = useParams()
  const navigate = useNavigate()
  const [project, setProject] = useState(null)
  const [rows, setRows] = useState([{ from: '', to: '' }, { from: '', to: '' }])
  const [caseSensitive, setCaseSensitive] = useState(false)
  const [preview, setPreview] = useState(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [stale, setStale] = useState(false)

  useEffect(() => {
    api.getProject(projectId).then(setProject).catch((e) => setError(e.message))
  }, [projectId])

  const validationError = useMemo(() => validatePairs(rows), [rows])
  const completeRows = useMemo(
    () => rows.filter((r) => (r.from || '').trim() && (r.to || '').trim()),
    [rows])

  // Re-run the server-side preview (debounced) against the saved transcript
  // whenever the valid pair set or the case-sensitivity toggle changes.
  useEffect(() => {
    if (!project || validationError) return undefined
    const payload = rows
      .filter((r) => (r.from || '').trim() && (r.to || '').trim())
      .map((r) => ({ from: r.from.trim(), to: r.to.trim() }))
    const timer = setTimeout(() => {
      setError('')
      setLoading(true)
      api.previewTypoCorrections(projectId, payload, caseSensitive)
        .then((data) => { setPreview(data); setStale(false) })
        .catch((e) => setError(e.message))
        .finally(() => setLoading(false))
    }, 350)
    return () => clearTimeout(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, project, rows, validationError, caseSensitive])

  const addRow = () => setRows((rs) => [...rs, { from: '', to: '' }])
  const patchRow = (i, patch) =>
    setRows((rs) => rs.map((r, j) => (j === i ? { ...r, ...patch } : r)))
  const removeRow = (i) => setRows((rs) => rs.filter((_, j) => j !== i))

  const runPreview = async () => {
    const payload = completeRows.map((r) => ({
      from: r.from.trim(), to: r.to.trim(),
    }))
    setLoading(true); setError('')
    try {
      const data = await api.previewTypoCorrections(
        projectId, payload, caseSensitive)
      setPreview(data)
      setStale(false)
      return data
    } catch (e) {
      setError(e.message)
      return null
    } finally {
      setLoading(false)
    }
  }

  const confirm = async () => {
    if (validationError || !preview || preview.summary.hits === 0) return
    const s = preview.summary
    if (!window.confirm(
      `确定应用错词批量校正？\n将改写 ${s.segments} 个片段中的 ` +
      `${s.hits} 处命中（${s.hitPairs} 个错词）。\n` +
      '此操作在一次事务中完成，片段 ID、时间、说话人与全部校注保持不变。'
    )) return
    setBusy(true); setError('')
    const payload = completeRows.map((r) => ({
      from: r.from.trim(), to: r.to.trim(),
    }))
    try {
      const res = await api.commitTypoCorrections(projectId, {
        pairs: payload,
        caseSensitive,
        version: preview.version,
      })
      navigate(`/projects/${projectId}`, {
        state: { typoCorrection: res.counts },
      })
    } catch (e) {
      if (e.status === 409) {
        // The saved transcript was edited / re-imported after the preview:
        // refuse and regenerate the preview before the user can commit.
        setStale(true)
        setError(`${e.message} 已为你重新生成预览，请核对后再次确认。`)
        const fresh = await runPreview()
        if (fresh) setStale(false)
      } else {
        setError(e.message)
      }
      setBusy(false)
    }
  }

  if (!project && !error) return <div className="page">加载中…</div>

  const s = preview?.summary
  const canCommit = !!preview && !validationError && s?.hits > 0 && !busy

  return (
    <div className="page typo-page">
      <div className="page-head">
        <div className="title-line">
          <Link to={`/projects/${projectId}`} className="back">
            ← 返回校注
          </Link>
          <h2>转写错词批量校正 · {project?.name}</h2>
        </div>
        <div className="head-actions">
          <Link className="btn" to={`/projects/${projectId}`}>
            打开时间轴校注
          </Link>
        </div>
      </div>
      {error && <div className="error">{error}</div>}
      {validationError && <div className="error">{validationError}</div>}
      {stale && <div className="error">预览已过期，正在重新生成…</div>}

      <div className="panel tc-rules">
        <h3>
          错词校正表
          <span className="muted small tc-panel-hint">
            逐行填写「错词 → 正词」，预览基于<strong>已保存</strong>的转写；
            同一位置优先最长错词，每个片段只扫描原文一次，替换出的文字不再继续
            匹配。空错词、空正词与重复错词需先修正。
          </span>
        </h3>
        <div className="tc-pair-list">
          {rows.map((row, i) => (
            <div className="tc-pair-row" key={i}>
              <input
                value={row.from}
                placeholder="错词（原文中的写法）"
                onChange={(e) => patchRow(i, { from: e.target.value })}
              />
              <span className="tc-arrow">→</span>
              <input
                value={row.to}
                placeholder="正词（校正为）"
                onChange={(e) => patchRow(i, { to: e.target.value })}
              />
              <button className="btn icon danger" onClick={() => removeRow(i)}
                title="删除此行">
                ✕
              </button>
            </div>
          ))}
        </div>
        <div className="tc-pair-actions">
          <button className="btn" onClick={addRow}>＋ 添加一行</button>
          <label className="tc-case">
            <input
              type="checkbox"
              checked={caseSensitive}
              onChange={(e) => setCaseSensitive(e.target.checked)}
            />
            区分大小写（默认不区分；中文无需勾选）
          </label>
        </div>
      </div>

      <div className="panel tc-preview">
        <h3>
          确认前预览：各片段命中与校正后文本
          {loading && <span className="muted small tc-panel-hint">更新中…</span>}
          {preview && (
            <span className="muted small tc-panel-hint">
              预览版本 {preview.version.slice(0, 12)}…
            </span>
          )}
        </h3>
        {preview ? (
          <>
            <div className="stats-grid">
              <div className="stat-box">
                <strong>有效错词</strong><span>{s.pairs}</span>
              </div>
              <div className={`stat-box ${s.hits ? 'has-conflict' : ''}`}>
                <strong>命中错词</strong><span>{s.hitPairs}</span>
              </div>
              <div className="stat-box">
                <strong>命中总次数</strong><span>{s.hits}</span>
              </div>
              <div className={`stat-box ${s.segments ? 'has-conflict' : ''}`}>
                <strong>影响片段</strong><span>{s.segments}</span>
              </div>
            </div>

            {preview.pairs.length > 0 && (
              <table className="run-table tc-pair-table">
                <thead>
                  <tr><th>错词</th><th></th><th>正词</th>
                    <th>命中次数</th><th>命中片段</th></tr>
                </thead>
                <tbody>
                  {preview.pairs.map((p) => (
                    <tr key={p.from}>
                      <td><strong>{p.from}</strong></td>
                      <td className="tc-arrow">→</td>
                      <td><strong>{p.to}</strong></td>
                      <td>{p.hits}</td>
                      <td>{p.segments}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}

            {preview.segments.length === 0 ? (
              <div className="empty-hint">
                当前已保存的转写中没有任何错词命中：请检查错词写法，或先在编辑页
                保存转写后再预览。
              </div>
            ) : (
              <div className="tc-segment-list">
                {preview.segments.map((seg) => (
                  <SegmentPreview key={`${seg.id}-${seg.index}`} seg={seg} />
                ))}
              </div>
            )}

            <div className="tc-confirm-row">
              <button
                className="btn primary big"
                disabled={!canCommit}
                onClick={confirm}
              >
                {busy ? '提交中…' : `✅ 确认校正（影响 ${s.segments} 个片段）`}
              </button>
              <span className="muted small">
                {s.hits === 0
                  ? '当前词表不会改动任何片段（可继续添加错词）。'
                  : '确认后只原子改写命中片段的 text 字段，片段 ID、起止时间、'
                    + '说话人与全部校注原样保留；随后返回编辑页查看结果。'
                    + '若期间转写被他人编辑或重新导入，将拒绝提交并要求重新预览。'}
              </span>
            </div>
          </>
        ) : (
          <div className="empty-hint">
            {validationError ? '修正上方错词表后显示预览。' : '正在生成预览…'}
          </div>
        )}
      </div>
    </div>
  )
}

function SegmentPreview({ seg }) {
  return (
    <div className="tc-segment">
      <div className="tc-segment-head">
        <span className="tc-seg-id">{seg.id}</span>
        <span className="muted small">
          {fmtTime(seg.start)} – {fmtTime(seg.end)}
          {seg.speaker ? ` · ${seg.speaker}` : ''}
        </span>
        <span className="badge">{seg.hits.length} 处</span>
      </div>
      <div className="tc-text tc-text-original">
        <span className="tc-text-label">原文</span>
        <HighlightedOriginal text={seg.originalText} hits={seg.hits} />
      </div>
      <div className="tc-text tc-text-corrected">
        <span className="tc-text-label">校正后</span>
        <HighlightedCorrected text={seg.originalText} hits={seg.hits} />
      </div>
    </div>
  )
}

/** Code-point-safe substring: Chinese / emoji keep a stable index per char. */
function chars(text) {
  return Array.from(text || '')
}

function HighlightedOriginal({ text, hits }) {
  const out = chars(text)
  const nodes = []
  let cursor = 0
  hits.forEach((h, i) => {
    if (h.start > cursor) nodes.push(out.slice(cursor, h.start).join(''))
    nodes.push(<mark key={`o${i}`} className="tc-hit">{out.slice(h.start, h.end).join('')}</mark>)
    cursor = h.end
  })
  if (cursor < out.length) nodes.push(out.slice(cursor).join(''))
  return <span>{nodes}</span>
}

function HighlightedCorrected({ text, hits }) {
  const out = chars(text)
  const nodes = []
  let cursor = 0 // position in the original text already consumed
  hits.forEach((h, i) => {
    if (h.start > cursor) nodes.push(out.slice(cursor, h.start).join(''))
    nodes.push(<mark key={`c${i}`} className="tc-fix">{h.to}</mark>)
    cursor = h.end
  })
  if (cursor < out.length) nodes.push(out.slice(cursor).join(''))
  return <span>{nodes}</span>
}
