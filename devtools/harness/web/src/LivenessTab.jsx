import { useState } from 'react'
import { FaceLivenessDetector } from '@aws-amplify/ui-react-liveness'
import { ThemeProvider } from '@aws-amplify/ui-react'
import '@aws-amplify/ui-react/styles.css'

async function credentialProvider() {
  const r = await fetch('/api/aws-creds')
  if (!r.ok) throw new Error(`creds fetch failed: ${r.status}`)
  const c = await r.json()
  return {
    accessKeyId: c.accessKeyId,
    secretAccessKey: c.secretAccessKey,
    sessionToken: c.sessionToken,
    expiration: new Date(c.expiration),
  }
}

export default function LivenessTab() {
  const [phase, setPhase] = useState('idle') // idle | starting | challenge | fetching | done | error
  const [session, setSession] = useState(null)
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)
  const [checkCount, setCheckCount] = useState(
    () => Number(localStorage.getItem('livenessChecks') || 0),
  )

  const bumpCount = () => {
    const n = checkCount + 1
    setCheckCount(n)
    localStorage.setItem('livenessChecks', String(n))
  }

  const start = async () => {
    setPhase('starting')
    setError(null)
    setResult(null)
    try {
      const r = await fetch('/api/liveness/sessions', { method: 'POST' })
      if (!r.ok) throw new Error(`session create failed: ${r.status} ${await r.text()}`)
      setSession(await r.json())
      setPhase('challenge')
    } catch (e) {
      setError(String(e))
      setPhase('error')
    }
  }

  const fetchResult = async () => {
    setPhase('fetching')
    try {
      const r = await fetch(`/api/liveness/sessions/${session.session_id}/result`)
      if (!r.ok) throw new Error(`result fetch failed: ${r.status} ${await r.text()}`)
      setResult(await r.json())
      bumpCount()
      setPhase('done')
    } catch (e) {
      setError(String(e))
      setPhase('error')
    }
  }

  return (
    <section>
      <p className="hint">
        Each completed check bills ~$0.015. Checks run so far on this browser: {checkCount}. Try
        your real face, then try a photo of a face on your phone screen — Rekognition should pass
        the first and fail the second.
      </p>

      {(phase === 'idle' || phase === 'done' || phase === 'error') && (
        <button className="primary" onClick={start}>
          Start liveness check
        </button>
      )}
      {phase === 'starting' && <p>Creating Rekognition session…</p>}

      {phase === 'challenge' && session && (
        <div className="liveness-box">
          <ThemeProvider>
            <FaceLivenessDetector
              sessionId={session.session_id}
              region={session.region}
              onAnalysisComplete={fetchResult}
              onError={(e) => {
                setError(e?.error?.message || JSON.stringify(e))
                setPhase('error')
              }}
              onUserCancel={() => setPhase('idle')}
              config={{ credentialProvider }}
            />
          </ThemeProvider>
        </div>
      )}

      {phase === 'fetching' && <p>Fetching verdict from Rekognition…</p>}

      {phase === 'done' && result && (
        <div className={`result-card ${result.passed ? 'pass' : 'fail'}`}>
          <h2>{result.passed ? 'PASSED — live person' : 'FAILED / below threshold'}</h2>
          <table>
            <tbody>
              <tr>
                <td>Status</td>
                <td>{result.status}</td>
              </tr>
              <tr>
                <td>Confidence</td>
                <td>{result.confidence?.toFixed(2)}</td>
              </tr>
              <tr>
                <td>Threshold (config)</td>
                <td>{result.threshold}</td>
              </tr>
              <tr>
                <td>Audit images</td>
                <td>{result.audit_image_count}</td>
              </tr>
            </tbody>
          </table>
          {result.reference_image && (
            <figure>
              <img src={result.reference_image} alt="reference frame from the liveness video" />
              <figcaption>
                Reference image — in production this exact frame is what gets enrolled via
                IndexFaces. Shown from memory, never saved.
              </figcaption>
            </figure>
          )}
        </div>
      )}

      {phase === 'error' && <pre className="error">{error}</pre>}
    </section>
  )
}
