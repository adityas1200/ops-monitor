import React, { useCallback, useEffect, useState } from 'react'
import { api } from '../api/client'
import { buildConnectionNotices } from '../utils/connectionHints'

const AWS_FIELDS = [
  ['region', 'Region', 'AWS region for Glue / Step Functions (optional)'],
  ['access_key_id', 'Access Key ID', 'IAM access key for live AWS telemetry'],
  ['secret_access_key', 'Secret Access Key', 'Stored locally; masked after save'],
  ['session_token', 'Session Token', 'Optional — for temporary STS credentials'],
]

const SF_REQUIRED = [
  ['account', 'Account', 'Snowflake account identifier (e.g. org-account.region)'],
  ['user', 'User', 'Login name — required for password and key-pair auth'],
  ['warehouse', 'Warehouse', 'Warehouse used for monitoring queries'],
  ['role', 'Role', 'Role with access to ACCOUNT_USAGE and DQ tables'],
]
const SF_OPTIONAL = [
  ['database', 'Default database', 'Optional session default'],
  ['schema', 'Default schema', 'Optional session default'],
]

const TASK_FIELDS = [
  ['monitor_database', 'Monitor database', 'Database name filter for SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY'],
  ['name_pattern', 'Task name pattern', 'SQL ILIKE pattern (e.g. TASK% or %ETL%)'],
  ['historical_months', 'Historical months', 'How far back ACCOUNT_USAGE history is scanned'],
  ['future_days', 'Future days', 'How many days ahead to include scheduled tasks'],
]

const DQ_FIELDS = [
  ['table_fqn', 'DQ summary table', 'Fully qualified table (DB.SCHEMA.TABLE)'],
]

const DEFAULT_TASKS = {
  monitor_database: 'CPH_DB_PROD',
  name_pattern: 'TASK%',
  historical_months: 2,
  future_days: 7,
}

const DEFAULT_DQ = {
  table_fqn: 'CPH_DB_PRE_PROD.MODEL_V2.DQM_VALIDATION_SUMMARY',
}

function resolveSfAuthMethod(authenticator) {
  const a = (authenticator || 'password').toLowerCase()
  if (a === 'externalbrowser' || a === 'sso') return 'sso'
  if (a.startsWith('http')) return 'sso'
  if (a === 'keypair' || a === 'key_pair' || a === 'private_key' || a === 'snowflake_jwt') return 'keypair'
  return 'password'
}

function resolveIdpUrl(authenticator) {
  const a = authenticator || ''
  return a.toLowerCase().startsWith('http') ? a : ''
}

function trimOrEmpty(v) {
  return (v ?? '').trim()
}

function isMaskedSecret(v) {
  return String(v || '').includes('***')
}

function normalizePemPaste(raw) {
  let text = String(raw || '')
  if (text.includes('\\n') || text.includes('\\r')) {
    text = text.replace(/\\r\\n/g, '\n').replace(/\\n/g, '\n').replace(/\\r/g, '\n')
  }
  return text
}

function onPrivateKeyPemChange(prevSf, nextRaw) {
  const nextPem = normalizePemPaste(nextRaw)
  const next = { ...prevSf, private_key_pem: nextPem }
  // If replacing a masked key with a real PEM, force re-entry of passphrase for encrypted keys.
  const wasMasked = isMaskedSecret(prevSf.private_key_pem)
  const isReal = nextPem.includes('BEGIN') && !isMaskedSecret(nextPem)
  if (wasMasked && isReal && isMaskedSecret(prevSf.private_key_passphrase)) {
    next.private_key_passphrase = ''
  }
  return next
}

function mergeMonitoringDefaults(defaults, values) {
  const out = { ...defaults }
  if (!values || typeof values !== 'object') return out
  for (const [k, v] of Object.entries(values)) {
    if (v === null || v === undefined) continue
    if (typeof v === 'string' && !v.trim()) continue
    out[k] = v
  }
  return out
}

function resolveTasks(tasks) {
  const merged = mergeMonitoringDefaults(DEFAULT_TASKS, tasks)
  return {
    monitor_database: trimOrEmpty(merged.monitor_database) || DEFAULT_TASKS.monitor_database,
    name_pattern: trimOrEmpty(merged.name_pattern) || DEFAULT_TASKS.name_pattern,
    historical_months: Math.max(1, Number(merged.historical_months) || DEFAULT_TASKS.historical_months),
    future_days: Math.max(1, Number(merged.future_days) || DEFAULT_TASKS.future_days),
  }
}

function resolveDq(dq) {
  const merged = mergeMonitoringDefaults(DEFAULT_DQ, dq)
  return {
    table_fqn: trimOrEmpty(merged.table_fqn) || DEFAULT_DQ.table_fqn,
  }
}

function snowflakePayload(sf, sfAuthMethod, idpUrl) {
  const base = {
    account: trimOrEmpty(sf.account),
    user: trimOrEmpty(sf.user),
    warehouse: trimOrEmpty(sf.warehouse),
    role: trimOrEmpty(sf.role),
    database: trimOrEmpty(sf.database),
    schema: trimOrEmpty(sf.schema),
    preprod_account: trimOrEmpty(sf.preprod_account),
  }
  if (sfAuthMethod === 'sso') {
    return {
      ...base,
      authenticator: trimOrEmpty(idpUrl) || 'sso',
      password: '',
      private_key_pem: '',
      private_key_passphrase: '',
    }
  }
  if (sfAuthMethod === 'keypair') {
    return {
      ...base,
      authenticator: 'keypair',
      password: '',
      private_key_pem: trimOrEmpty(sf.private_key_pem),
      private_key_passphrase: trimOrEmpty(sf.private_key_passphrase),
    }
  }
  return {
    ...base,
    authenticator: 'password',
    password: trimOrEmpty(sf.password),
    private_key_pem: '',
    private_key_passphrase: '',
  }
}

function validateSnowflake(sf, sfAuthMethod) {
  const configuring = [
    'account', 'user', 'warehouse', 'role', 'database', 'schema', 'preprod_account',
    'password', 'private_key_pem', 'private_key_passphrase',
  ].some((k) => trimOrEmpty(sf[k]))
  const passwordMasked = isMaskedSecret(sf.password)
  const hasPassword = trimOrEmpty(sf.password) && !passwordMasked
  const keyMasked = isMaskedSecret(sf.private_key_pem)
  const hasKey = trimOrEmpty(sf.private_key_pem) && !keyMasked
  if (!configuring && !hasPassword && !hasKey && !passwordMasked && !keyMasked) return null

  const missing = ['account', 'warehouse', 'role'].filter((k) => !trimOrEmpty(sf[k]))
  if (missing.length) return `Snowflake requires: ${missing.join(', ')}`

  if (sfAuthMethod === 'password') {
    if (!trimOrEmpty(sf.user)) return 'User is required for password login.'
    if (!hasPassword && !passwordMasked) {
      return 'Password is required for password login (or switch to SSO / key-pair).'
    }
  }
  if (sfAuthMethod === 'keypair') {
    if (!trimOrEmpty(sf.user)) return 'User is required for key-pair login.'
    if (!hasKey && !keyMasked) return 'Private key PEM is required for key-pair login.'
  }
  return null
}

function taskPreview(tasks) {
  const db = tasks.monitor_database || '…'
  const pattern = tasks.name_pattern || '…'
  const months = tasks.historical_months ?? '…'
  const days = tasks.future_days ?? '…'
  return `-- Tasks tab queries
FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
WHERE DATABASE_NAME = '${db}'
  AND NAME ILIKE '${pattern}'
  AND SCHEDULED_TIME >= DATEADD(MONTH, -${months}, CURRENT_TIMESTAMP())

-- Plus scheduled tasks up to ${days} days ahead`
}

function dqPreview(dq) {
  const table = dq.table_fqn || '…'
  return `SELECT QC_ID, CHECK_TYPE, STATUS, PASS_COUNT, FAIL_COUNT
FROM ${table}
ORDER BY RUN_DATE DESC
LIMIT 500`
}

function ConfigField({ label, hint, type = 'text', value, onChange, disabled }) {
  return (
    <div className="field">
      <label>{label}</label>
      <input
        type={type}
        value={value ?? ''}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
      />
      {hint && <span className="field-hint">{hint}</span>}
    </div>
  )
}

function StatusPill({ name, status, detail }) {
  const ok = status === 'connected'
  return (
    <div className={`status-pill${ok ? ' ok' : ' err'}`} title={detail || ''}>
      <span className="status-dot" aria-hidden="true" />
      <span className="status-name">{name}</span>
      <span className="status-val">{status}</span>
    </div>
  )
}

function buildSettingsPayload(aws, sf, sfAuthMethod, idpUrl, tasks, dq) {
  return {
    aws: {
      region: trimOrEmpty(aws.region),
      access_key_id: trimOrEmpty(aws.access_key_id),
      secret_access_key: trimOrEmpty(aws.secret_access_key),
      session_token: trimOrEmpty(aws.session_token),
    },
    snowflake: snowflakePayload(sf, sfAuthMethod, idpUrl),
    monitoring: {
      tasks: resolveTasks(tasks),
      dq: resolveDq(dq),
    },
  }
}

function connSummaryLines(c) {
  if (!c?.services?.length) return []
  const want = []
  if (c.platforms?.snowflake) want.push('Snowflake')
  if (c.platforms?.aws) want.push('AWS STS')
  // If nothing is marked configured, still show primary probes for the failure popup.
  const names = want.length ? want : ['Snowflake', 'AWS STS']
  return c.services
    .filter((s) => names.includes(s.name))
    .map((s) => `${s.name}: ${s.status}${s.detail ? ` — ${s.detail}` : ''}`)
}

export default function Settings({ onSaved, onReportError }) {
  const [section, setSection] = useState('connections')
  const [aws, setAws] = useState({})
  const [sf, setSf] = useState({})
  const [tasks, setTasks] = useState(() => ({ ...DEFAULT_TASKS }))
  const [dq, setDq] = useState(() => ({ ...DEFAULT_DQ }))
  const [sfAuthMethod, setSfAuthMethod] = useState('password')
  const [idpUrl, setIdpUrl] = useState('')
  const [status, setStatus] = useState(null)
  const [conn, setConn] = useState(null)
  const [connLoading, setConnLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [loaded, setLoaded] = useState(false)
  const [connModal, setConnModal] = useState(null)

  const reportConnIssues = (c, inputs) => {
    buildConnectionNotices(c, inputs).forEach((n) => {
      onReportError?.({ tab: 'Settings', action: 'test connection', error: n.text })
    })
  }

  const applySettings = useCallback((s) => {
    const snowflake = s.snowflake || {}
    setAws(s.aws || {})
    setSf({
      account: snowflake.account || '',
      user: snowflake.user || '',
      warehouse: snowflake.warehouse || '',
      role: snowflake.role || '',
      database: snowflake.database || '',
      schema: snowflake.schema || '',
      preprod_account: snowflake.preprod_account || '',
      password: snowflake.password || '',
      private_key_pem: snowflake.private_key_pem || '',
      private_key_passphrase: snowflake.private_key_passphrase || '',
    })
    setSfAuthMethod(resolveSfAuthMethod(snowflake.authenticator))
    setIdpUrl(resolveIdpUrl(snowflake.authenticator))
    const mon = s.monitoring || {}
    setTasks(resolveTasks(mon.tasks))
    setDq(resolveDq(mon.dq))
    setLoaded(true)
    return {
      aws: s.aws || {},
      snowflake,
      sfAuthMethod: resolveSfAuthMethod(snowflake.authenticator),
      idpUrl: resolveIdpUrl(snowflake.authenticator),
    }
  }, [])

  useEffect(() => {
    api.getSettings()
      .then((s) => {
        const inputs = applySettings(s)
        setConnLoading(true)
        return api.connectivity()
          .then((c) => {
            setConn(c)
            buildConnectionNotices(c, inputs).forEach((n) => {
              onReportError?.({ tab: 'Settings', action: 'test connection', error: n.text })
            })
          })
          .finally(() => setConnLoading(false))
      })
      .catch((e) => onReportError?.({ tab: 'Settings', action: 'load settings', error: e.message }))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const persistSettings = (payload, { fromModal = false } = {}) => {
    setSaving(true)
    return api.saveSettings(payload)
      .then((r) => {
        setStatus({
          type: 'ok',
          text: fromModal
            ? 'Connections saved. Dashboard will use these credentials.'
            : 'Saved. Refresh the Dashboard to load data with the new configuration.',
        })
        onSaved?.(r.platforms || {})
        if (r.settings) applySettings(r.settings)
        setConnModal(null)
        return api.connectivity().then((c) => {
          setConn(c)
          return r
        })
      })
      .catch((e) => {
        setStatus({ type: 'error', text: e.message })
        onReportError?.({ tab: 'Settings', action: 'save settings', error: e.message })
        throw e
      })
      .finally(() => setSaving(false))
  }

  const save = () => {
    const sfErr = validateSnowflake(sf, sfAuthMethod)
    if (sfErr) {
      setStatus({ type: 'error', text: sfErr })
      onReportError?.({ tab: 'Settings', action: 'save settings', error: sfErr })
      return
    }
    persistSettings(buildSettingsPayload(aws, sf, sfAuthMethod, idpUrl, tasks, dq))
  }

  const testConnections = () => {
    const sfErr = validateSnowflake(sf, sfAuthMethod)
    if (sfErr) {
      setConnModal({
        success: false,
        title: 'Connection failed',
        message: sfErr,
        lines: [],
      })
      onReportError?.({ tab: 'Settings', action: 'test connection', error: sfErr })
      return
    }
    const payload = buildSettingsPayload(aws, sf, sfAuthMethod, idpUrl, tasks, dq)
    const inputs = {
      aws: payload.aws,
      snowflake: payload.snowflake,
      sfAuthMethod,
      idpUrl,
    }
    setConnLoading(true)
    api.testConnectivity(payload)
      .then((c) => {
        setConn(c)
        reportConnIssues(c, inputs)
        const lines = connSummaryLines(c)
        if (c.can_save) {
          setConnModal({
            success: true,
            title: 'Connection successful',
            message: 'Configured platforms connected. Save these settings if you want to keep them.',
            lines,
            payload,
          })
        } else {
          const failed = (c.services || []).filter(
            (s) => (s.name === 'Snowflake' || s.name === 'AWS STS') && s.status !== 'connected',
          )
          const detail = failed.map((s) => s.detail).filter(Boolean).join(' ')
            || 'No configured platform connected successfully.'
          setConnModal({
            success: false,
            title: 'Connection failed',
            message: detail,
            lines,
            hints: failed.flatMap((s) => s.hints || []).slice(0, 4),
          })
        }
      })
      .catch((e) => {
        setConnModal({
          success: false,
          title: 'Connection failed',
          message: e.message || 'Could not test connections.',
          lines: [],
        })
        onReportError?.({ tab: 'Settings', action: 'test connection', error: e.message })
      })
      .finally(() => setConnLoading(false))
  }

  const resetTasks = () => setTasks({ ...DEFAULT_TASKS })
  const resetDq = () => setDq({ ...DEFAULT_DQ })

  const displayTasks = resolveTasks(tasks)
  const displayDq = resolveDq(dq)

  const snowflakeOn = conn?.platforms?.snowflake
  const awsOn = conn?.platforms?.aws

  return (
    <div className="settings-page">
      <div className="card settings-status-card">
        <div className="settings-status-head">
          <div>
            <h3>Platform status</h3>
            <p className="muted settings-lead">
              Live values below drive the Dashboard Tasks and Data Quality tabs after you save.
            </p>
          </div>
          <button
            type="button"
            className={`btn sec${connLoading ? ' is-loading' : ''}`}
            disabled={connLoading || saving}
            onClick={testConnections}
          >
            {connLoading ? 'Testing…' : 'Test connections'}
          </button>
        </div>
        {conn ? (
          <div className="status-grid">
            {conn.services.map((s) => (
              <StatusPill key={s.name} name={s.name} status={s.status} detail={s.detail} />
            ))}
            <div className="status-pill neutral">
              <span className="status-name">Snowflake data</span>
              <span className={`status-val${snowflakeOn ? ' ok-text' : ''}`}>{snowflakeOn ? 'enabled' : 'off'}</span>
            </div>
            <div className="status-pill neutral">
              <span className="status-name">AWS data</span>
              <span className={`status-val${awsOn ? ' ok-text' : ''}`}>{awsOn ? 'enabled' : 'off'}</span>
            </div>
          </div>
        ) : (
          <span className="muted">{loaded ? 'No connectivity data.' : 'Loading settings…'}</span>
        )}
      </div>

      <div className="subtabs settings-subtabs">
        {[
          ['connections', 'Connections'],
          ['tasks', 'Task monitoring'],
          ['dq', 'Data quality'],
        ].map(([id, label]) => (
          <button
            key={id}
            type="button"
            className={`subtab ${section === id ? 'active' : ''}`}
            onClick={() => setSection(id)}
          >
            {label}
          </button>
        ))}
      </div>

      {section === 'connections' && (
        <div className="grid2">
          <div className="card">
            <h3>AWS connection</h3>
            <p className="muted settings-section-desc">
              Optional — enables Glue and Step Functions telemetry on the Dashboard.
            </p>
            {AWS_FIELDS.map(([k, label, hint]) => (
              <ConfigField
                key={k}
                label={label}
                hint={hint}
                type={k.includes('secret') || k.includes('token') ? 'password' : 'text'}
                value={aws[k] || ''}
                onChange={(v) => setAws({ ...aws, [k]: v })}
              />
            ))}
          </div>

          <div className="card">
            <h3>Snowflake connection</h3>
            <p className="muted settings-section-desc">
              Required for Tasks and Data Quality. Credentials are stored locally in{' '}
              <code>connection_settings.json</code>.
            </p>
            <div className="field">
              <label>Login method</label>
              <select value={sfAuthMethod} onChange={(e) => setSfAuthMethod(e.target.value)}>
                <option value="password">Password (non-SSO)</option>
                <option value="sso">SSO (browser)</option>
                <option value="keypair">Key pair (JWT)</option>
              </select>
            </div>

            {SF_REQUIRED.slice(0, 2).map(([k, label, hint]) => (
              <ConfigField
                key={k}
                label={label}
                hint={hint}
                value={sf[k] || ''}
                onChange={(v) => setSf({ ...sf, [k]: v })}
              />
            ))}

            {sfAuthMethod === 'password' && (
              <ConfigField
                label="Password"
                hint="Leave blank on save to keep the existing password (masked values are not re-sent)."
                type="password"
                value={sf.password || ''}
                onChange={(v) => setSf({ ...sf, password: v })}
              />
            )}

            {sfAuthMethod === 'sso' && (
              <div className="field">
                <label>IdP URL (optional)</label>
                <input
                  type="text"
                  placeholder="Leave blank for browser SSO"
                  value={idpUrl}
                  onChange={(e) => setIdpUrl(e.target.value)}
                />
                <span className="field-hint">
                  SSO opens a browser on the backend machine — complete sign-in within 2 minutes.
                </span>
              </div>
            )}

            {sfAuthMethod === 'keypair' && (
              <>
                <div className="field">
                  <label>Private key (PEM)</label>
                  <textarea
                    rows={6}
                    spellCheck={false}
                    placeholder={'-----BEGIN PRIVATE KEY-----\n…\n-----END PRIVATE KEY-----'}
                    value={sf.private_key_pem || ''}
                    onChange={(e) => setSf(onPrivateKeyPemChange(sf, e.target.value))}
                  />
                  <span className="field-hint">
                    PKCS#8 PEM for the Snowflake user (BEGIN PRIVATE KEY / ENCRYPTED PRIVATE KEY).
                    Single-line pastes with \n are accepted. Leave as *** on save to keep the stored key.
                    Register the matching public key with ALTER USER … SET RSA_PUBLIC_KEY.
                  </span>
                </div>
                <ConfigField
                  label="Private key passphrase"
                  hint="Only if the PEM is encrypted. Leave blank for unencrypted keys (or keep *** to retain)."
                  type="password"
                  value={sf.private_key_passphrase || ''}
                  onChange={(v) => setSf({ ...sf, private_key_passphrase: v })}
                />
              </>
            )}

            {SF_REQUIRED.slice(2).map(([k, label, hint]) => (
              <ConfigField
                key={k}
                label={label}
                hint={hint}
                value={sf[k] || ''}
                onChange={(v) => setSf({ ...sf, [k]: v })}
              />
            ))}

            {SF_OPTIONAL.map(([k, label, hint]) => (
              <ConfigField
                key={k}
                label={label}
                hint={hint}
                value={sf[k] || ''}
                onChange={(v) => setSf({ ...sf, [k]: v })}
              />
            ))}
          </div>
        </div>
      )}

      {section === 'tasks' && (
        <div className="settings-split">
          <div className="card">
            <div className="settings-card-head">
              <h3>Task monitoring targets</h3>
              <button type="button" className="btn sec" onClick={resetTasks}>Reset defaults</button>
            </div>
            <p className="muted settings-section-desc">
              These map to the Snowflake queries behind the Dashboard <strong>Tasks</strong> tab.
              {!snowflakeOn && ' Connect Snowflake first to load live task history.'}
            </p>
            {TASK_FIELDS.map(([k, label, hint]) => (
              <ConfigField
                key={k}
                label={label}
                hint={hint}
                type={k === 'historical_months' || k === 'future_days' ? 'number' : 'text'}
                value={displayTasks[k] ?? ''}
                onChange={(v) => setTasks({ ...displayTasks, [k]: v })}
              />
            ))}
          </div>
          <div className="card config-preview-card">
            <h3>Active query preview</h3>
            <p className="muted settings-section-desc">What the backend runs after you save.</p>
            <pre className="config-preview">{taskPreview(displayTasks)}</pre>
            <dl className="config-summary">
              <div><dt>Database</dt><dd><code>{displayTasks.monitor_database || '—'}</code></dd></div>
              <div><dt>Pattern</dt><dd><code>{displayTasks.name_pattern || '—'}</code></dd></div>
              <div><dt>Lookback</dt><dd>{displayTasks.historical_months} month(s)</dd></div>
              <div><dt>Schedule horizon</dt><dd>{displayTasks.future_days} day(s)</dd></div>
            </dl>
          </div>
        </div>
      )}

      {section === 'dq' && (
        <div className="settings-split">
          <div className="card">
            <div className="settings-card-head">
              <h3>Data quality sources</h3>
              <button type="button" className="btn sec" onClick={resetDq}>Reset defaults</button>
            </div>
            <p className="muted settings-section-desc">
              Points the Dashboard <strong>Data Quality</strong> tab at your validation summary table.
              {!snowflakeOn && ' Connect Snowflake first to load DQ results.'}
            </p>
            {DQ_FIELDS.map(([k, label, hint]) => (
              <ConfigField
                key={k}
                label={label}
                hint={hint}
                value={displayDq[k] ?? ''}
                onChange={(v) => setDq({ ...displayDq, [k]: v })}
              />
            ))}
          </div>
          <div className="card config-preview-card">
            <h3>Active query preview</h3>
            <p className="muted settings-section-desc">Rows are filtered by date range.</p>
            <pre className="config-preview">{dqPreview(displayDq)}</pre>
            <dl className="config-summary">
              <div><dt>Table</dt><dd><code>{displayDq.table_fqn || '—'}</code></dd></div>
            </dl>
          </div>
        </div>
      )}

      <div className="card settings-actions">
        <button type="button" className="btn" onClick={save} disabled={saving || connLoading}>
          {saving ? 'Saving…' : 'Save all settings'}
        </button>
        {status && (
          <span className={`settings-status-msg ${status.type === 'error' ? 'err-text' : 'ok-text'}`}>
            {status.text}
          </span>
        )}
        <p className="muted settings-footnote">
          Prefer <strong>Test connections</strong> first — on success you can save from the popup.
          Save all still writes connections and monitoring targets together.
        </p>
      </div>

      {connModal && (
        <div
          className="conn-modal-backdrop"
          role="presentation"
          onClick={() => !saving && setConnModal(null)}
        >
          <div
            className={`conn-modal card${connModal.success ? ' conn-modal-ok' : ' conn-modal-err'}`}
            role="dialog"
            aria-modal="true"
            aria-labelledby="conn-modal-title"
            onClick={(e) => e.stopPropagation()}
          >
            <h3 id="conn-modal-title">{connModal.title}</h3>
            <p className="conn-modal-msg">{connModal.message}</p>
            {connModal.lines?.length > 0 && (
              <ul className="conn-modal-lines">
                {connModal.lines.map((line) => (
                  <li key={line}>{line}</li>
                ))}
              </ul>
            )}
            {connModal.hints?.length > 0 && (
              <ul className="conn-modal-hints">
                {connModal.hints.map((h) => (
                  <li key={h}>{h}</li>
                ))}
              </ul>
            )}
            <div className="conn-modal-actions">
              {connModal.success ? (
                <>
                  <button
                    type="button"
                    className="btn sec"
                    disabled={saving}
                    onClick={() => setConnModal(null)}
                  >
                    Cancel
                  </button>
                  <button
                    type="button"
                    className="btn"
                    disabled={saving}
                    onClick={() => persistSettings(connModal.payload, { fromModal: true })}
                  >
                    {saving ? 'Saving…' : 'Save connections'}
                  </button>
                </>
              ) : (
                <button type="button" className="btn" onClick={() => setConnModal(null)}>
                  Close
                </button>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
