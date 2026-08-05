import { useState } from 'react'

export default function HiveTab() {
  const [file, setFile] = useState(null)
  const [url, setUrl] = useState('')
  const [busy, setBusy] = useState(false)
  const [resp, setResp] = useState(null)
  const [error, setError] = useState(null)

  const search = async () => {
    setBusy(true)
    setError(null)
    setResp(null)
    try {
      const form = new FormData()
      if (file) form.append('media', file)
      else if (url.trim()) form.append('url', url.trim())
      else throw new Error('pick an image file or paste an image URL')

      const r = await fetch('/api/hive/search', { method: 'POST', body: form })
      const body = await r.json()
      if (!r.ok) throw new Error(JSON.stringify(body))
      setResp(body)
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section>
      <p className="hint">
        Sends the image to Hive&apos;s sync task endpoint with your real API key (server-side).
        Response is shown verbatim below — the raw payload is what the Phase 4 adapter will store
        as <code>raw_payload</code>.
      </p>

      <div className="hive-form">
        <label>
          Image file:{' '}
          <input
            type="file"
            accept="image/jpeg,image/png,image/webp,image/gif"
            onChange={(e) => setFile(e.target.files?.[0] || null)}
          />
        </label>
        <span className="or">or</span>
        <label>
          Image URL:{' '}
          <input
            type="url"
            placeholder="https://…/photo.jpg"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            disabled={!!file}
          />
        </label>
        <button className="primary" onClick={search} disabled={busy}>
          {busy ? 'Searching…' : 'Search Hive'}
        </button>
      </div>

      {error && <pre className="error">{error}</pre>}

      {resp && (
        <div className="result-card">
          <h2>
            HTTP {resp.http_status} — {resp.matches.length} match-like object
            {resp.matches.length === 1 ? '' : 's'} found
          </h2>
          {resp.matches.length > 0 && (
            <table>
              <thead>
                <tr>
                  <th>#</th>
                  <th>URL</th>
                  <th>Score</th>
                </tr>
              </thead>
              <tbody>
                {resp.matches.map((m, i) => {
                  const u = m.url || m.URL || (m.backlinks && m.backlinks[0]) || ''
                  const s = m.score ?? m.similarity_score ?? m.Score ?? ''
                  return (
                    <tr key={i}>
                      <td>{i + 1}</td>
                      <td>
                        {u ? (
                          <a href={u} target="_blank" rel="noreferrer">
                            {String(u).slice(0, 90)}
                          </a>
                        ) : (
                          '—'
                        )}
                      </td>
                      <td>{typeof s === 'number' ? s.toFixed(3) : String(s)}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          )}
          <details>
            <summary>Raw payload</summary>
            <pre>{JSON.stringify(resp.raw_payload, null, 2)}</pre>
          </details>
        </div>
      )}
    </section>
  )
}
