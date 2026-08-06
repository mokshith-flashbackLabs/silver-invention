import { useState } from 'react'

const KIND_LABELS = {
  fullMatchingImages: 'exact copy',
  partialMatchingImages: 'crop / variant',
  pagesWithMatchingImages: 'page with match',
}

export default function GoogleTab() {
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

      const r = await fetch('/api/google/search', { method: 'POST', body: form })
      const body = await r.json()
      if (!r.ok) throw new Error(body.detail || JSON.stringify(body))
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
        Google Cloud Vision <code>WEB_DETECTION</code> — same image-search kind as Hive, different
        index. First 1,000 lookups/month are free, then ~$0.0035 each. Run the same image through
        both tabs to compare coverage.
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
          {busy ? 'Searching…' : 'Search Google'}
        </button>
      </div>

      {error && <pre className="error">{error}</pre>}

      {resp && (
        <div className="result-card">
          <h2>
            HTTP {resp.http_status} — {resp.matches.length} match
            {resp.matches.length === 1 ? '' : 'es'}, {resp.similar_count} visually similar
          </h2>
          {resp.best_guess?.length > 0 && (
            <p className="hint">Google&apos;s best guess: {resp.best_guess.join(', ')}</p>
          )}
          {resp.matches.length > 0 && (
            <table>
              <thead>
                <tr>
                  <th>#</th>
                  <th>Kind</th>
                  <th>URL</th>
                </tr>
              </thead>
              <tbody>
                {resp.matches.map((m, i) => (
                  <tr key={i}>
                    <td>{i + 1}</td>
                    <td>{KIND_LABELS[m.kind] || m.kind}</td>
                    <td>
                      {m.url ? (
                        <a href={m.url} target="_blank" rel="noreferrer">
                          {String(m.url).slice(0, 90)}
                        </a>
                      ) : (
                        '—'
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {resp.entities?.length > 0 && (
            <details>
              <summary>Web entities ({resp.entities.length})</summary>
              <table>
                <tbody>
                  {resp.entities.map((e, i) => (
                    <tr key={i}>
                      <td>{e.description}</td>
                      <td>{typeof e.score === 'number' ? e.score.toFixed(3) : ''}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </details>
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
