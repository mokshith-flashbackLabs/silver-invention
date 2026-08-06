import { useState } from 'react'
import LivenessTab from './LivenessTab.jsx'
import HiveTab from './HiveTab.jsx'
import GoogleTab from './GoogleTab.jsx'

export default function App() {
  const [tab, setTab] = useState('liveness')

  return (
    <div className="shell">
      <header>
        <h1>ImageShield local harness</h1>
        <p className="sub">
          Real AWS Face Liveness (us-east-1) + real Hive web search. Dev tool — nothing persists.
        </p>
        <nav>
          <button
            className={tab === 'liveness' ? 'active' : ''}
            onClick={() => setTab('liveness')}
          >
            Face Liveness
          </button>
          <button className={tab === 'hive' ? 'active' : ''} onClick={() => setTab('hive')}>
            Hive search
          </button>
          <button className={tab === 'google' ? 'active' : ''} onClick={() => setTab('google')}>
            Google web detection
          </button>
        </nav>
      </header>
      <main>
        {tab === 'liveness' && <LivenessTab />}
        {tab === 'hive' && <HiveTab />}
        {tab === 'google' && <GoogleTab />}
      </main>
    </div>
  )
}
