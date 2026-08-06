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
  const [mode, setMode] = useState('service') // service | direct
  return (
    <section>
      <div className="challenge-picker">
        <label>
          <input type="radio" checked={mode === 'service'} onChange={() => setMode('service')} />{' '}
          Via ImageShield service (step 3 — real endpoints, harness plays the proxy)
        </label>
        <label>
          <input type="radio" checked={mode === 'direct'} onChange={() => setMode('direct')} />{' '}
          Direct AWS (original spike)
        </label>
      </div>
      {mode === 'service' ? <ServiceLiveness /> : <DirectLiveness />}
    </section>
  )
}

// ── Service mode: Client -> harness(-as-proxy) -> ImageShield service ────────

function ServiceLiveness() {
  const [phase, setPhase] = useState('idle') // idle | starting | challenge | fetching | done | error
  const [userRef, setUserRef] = useState(
    () => localStorage.getItem('livenessUserRef') || crypto.randomUUID(),
  )
  const [session, setSession] = useState(null)
  const [result, setResult] = useState(null)
  const [status, setStatus] = useState(null)
  const [error, setError] = useState(null)

  localStorage.setItem('livenessUserRef', userRef)

  const fail = (payload) => {
    setError(typeof payload === 'string' ? payload : JSON.stringify(payload, null, 2))
    setPhase('error')
  }

  const start = async () => {
    setPhase('starting')
    setError(null)
    setResult(null)
    setStatus(null)
    try {
      const r = await fetch('/api/service/liveness/sessions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_ref: userRef }),
      })
      const body = await r.json()
      if (r.status !== 201) return fail(body)
      setSession(body)
      setPhase('challenge')
    } catch (e) {
      fail(String(e))
    }
  }

  const postResult = async (reuseKey) => {
    setPhase('fetching')
    try {
      const r = await fetch(`/api/service/liveness/${session.session_id}/result`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reuse_key: reuseKey }),
      })
      const body = await r.json()
      if (r.status !== 200) {
        setResult({ http_status: r.status, ...body })
        setPhase('done')
        return
      }
      setResult({ http_status: 200, ...body })
      setPhase('done')
    } catch (e) {
      fail(String(e))
    }
  }

  const getStatus = async () => {
    const r = await fetch(`/api/service/liveness/${session.session_id}`)
    setStatus({ http_status: r.status, ...(await r.json()) })
  }

  return (
    <>
      <p className="hint">
        Runs the real step-3 lifecycle: POST /v1/liveness/sessions, the on-device challenge, then
        POST /v1/liveness/&#123;id&#125;/result which persists the ReferenceImage through fake
        presigned URLs. Try a real face (should pass), then a photo of a face on another screen
        (should fail). Repeat creates after a pass demonstrate the 409; six creates in 24h the 429.
      </p>

      <p className="hint">
        user_ref: <code>{userRef}</code>{' '}
        <button onClick={() => setUserRef(crypto.randomUUID())}>new user_ref</button>
      </p>

      {(phase === 'idle' || phase === 'done' || phase === 'error') && (
        <button className="primary" onClick={start}>
          Start service liveness session
        </button>
      )}
      {phase === 'starting' && <p>Creating session via service…</p>}

      {phase === 'challenge' && session && (
        <div className="liveness-box">
          <p className="hint">
            service session <code>{session.session_id}</code> · provider session{' '}
            <code>{session.provider_session_id}</code> · expires {session.expires_at}
          </p>
          <ThemeProvider>
            <FaceLivenessDetector
              sessionId={session.provider_session_id}
              region={session.region}
              onAnalysisComplete={() => postResult(false)}
              onError={(e) => fail(e?.error?.message || JSON.stringify(e))}
              onUserCancel={() => setPhase('idle')}
              config={{ credentialProvider }}
            />
          </ThemeProvider>
        </div>
      )}

      {phase === 'fetching' && <p>Posting result to the service…</p>}

      {phase === 'done' && result && (
        <div
          className={`result-card ${result.http_status === 200 && result.status === 'passed' ? 'pass' : 'fail'}`}
        >
          <h2>
            {result.http_status === 200
              ? result.status === 'passed'
                ? 'PASSED — live person, reference image persisted'
                : 'FAILED — spoof or below threshold'
              : `HTTP ${result.http_status}`}
          </h2>
          <pre>{JSON.stringify(result, null, 2)}</pre>
          {result.reference_image_url && (
            <figure>
              <img src={result.reference_image_url} alt="reference frame persisted via presigned PUT" />
              <figcaption>
                Fetched back from the stored reference_image_uri — this object existing is the
                step-3 "done when" proof. In production this is the proxy's S3.
              </figcaption>
            </figure>
          )}
          <div className="challenge-picker">
            <button onClick={() => postResult(true)}>Retry result (same key — expect 200 replay)</button>
            <button onClick={() => postResult(false)}>Replay result (new key — expect 410)</button>
            <button onClick={getStatus}>GET status</button>
          </div>
          {status && <pre>{JSON.stringify(status, null, 2)}</pre>}
        </div>
      )}

      {phase === 'error' && <pre className="error">{error}</pre>}
    </>
  )
}

// ── Direct mode: the original spike, straight at Rekognition ────────────────

function DirectLiveness() {
  const [phase, setPhase] = useState('idle') // idle | starting | challenge | fetching | done | error
  const [session, setSession] = useState(null)
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)
  const [checkCount, setCheckCount] = useState(
    () => Number(localStorage.getItem('livenessChecks') || 0),
  )
  const [challenge, setChallenge] = useState('FaceMovementAndLightChallenge')

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
      const r = await fetch(`/api/liveness/sessions?challenge=${challenge}`, { method: 'POST' })
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
    <>
      <p className="hint">
        Each completed check bills ~$0.015. Checks run so far on this browser: {checkCount}. Try
        your real face, then try a photo of a face on your phone screen — Rekognition should pass
        the first and fail the second.
      </p>

      {(phase === 'idle' || phase === 'done' || phase === 'error') && (
        <>
          <div className="challenge-picker">
            <label>
              <input
                type="radio"
                checked={challenge === 'FaceMovementAndLightChallenge'}
                onChange={() => setChallenge('FaceMovementAndLightChallenge')}
              />{' '}
              Flash challenge (color lights, highest accuracy)
            </label>
            <label>
              <input
                type="radio"
                checked={challenge === 'FaceMovementChallenge'}
                onChange={() => setChallenge('FaceMovementChallenge')}
              />{' '}
              No-flash challenge (movement only, faster, photosensitivity-safe)
            </label>
          </div>
          <button className="primary" onClick={start}>
            Start liveness check
          </button>
        </>
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
    </>
  )
}
