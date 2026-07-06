function uniq(items) {
  return [...new Set(items.filter(Boolean))]
}

function awsHints(service, aws = {}) {
  const detail = (service.detail || '').toLowerCase()
  const hints = []

  if (!aws.access_key_id?.trim()) {
    hints.push('Enter your Access Key ID under Settings → AWS.')
  }
  if (!aws.secret_access_key?.trim() || String(aws.secret_access_key).includes('***')) {
    hints.push('Provide the full Secret Access Key — masked values (with ***) are not re-sent when you save.')
  }
  if (!aws.region?.trim()) {
    hints.push('Set the AWS Region (e.g. us-east-1) to match your account.')
  }
  if (detail.includes('invalidaccesskey') || detail.includes('invalidclienttokenid')) {
    hints.push('The Access Key ID looks invalid — confirm it is active and copied without extra spaces.')
  }
  if (detail.includes('signature') || detail.includes('secret access key')) {
    hints.push('The Secret Access Key does not match the Access Key ID — re-enter both from IAM.')
  }
  if (detail.includes('expired') || detail.includes('token')) {
    hints.push('Your session token may be expired — generate a new one or clear the Session Token field.')
  }
  if (detail.includes('not authorized') || detail.includes('accessdenied')) {
    hints.push('The IAM user/role needs permissions for STS, Glue, Step Functions, CloudWatch, and DynamoDB.')
  }
  if (detail.includes('region')) {
    hints.push('Double-check the Region matches where your AWS resources are deployed.')
  }
  if (!hints.length) {
    hints.push('Review Access Key ID, Secret Access Key, Region, and IAM permissions in Settings → AWS.')
  }
  return uniq(hints)
}

function snowflakeHints(service, sf = {}, sfAuthMethod = 'password', idpUrl = '') {
  const detail = (service.detail || '').toLowerCase()
  const hints = []
  const isSso = sfAuthMethod === 'sso'

  if (!sf.account?.trim()) {
    hints.push('Enter your Snowflake Account under Settings → Snowflake.')
  }
  if (!sf.user?.trim()) {
    hints.push('Enter your Snowflake User / login name.')
  }
  if (!sf.warehouse?.trim()) {
    hints.push('Enter the Warehouse name (required).')
  }
  if (!sf.role?.trim()) {
    hints.push('Enter the Role name (required).')
  }
  if (!isSso) {
    if (!sf.password?.trim() || String(sf.password).includes('***')) {
      hints.push('Provide your Snowflake password — masked values (with ***) are not re-sent when you save.')
    }
    if (detail.includes('incorrect username') || detail.includes('password') || detail.includes('authentication')) {
      hints.push('Verify the username and password are correct for the account.')
    }
  } else {
    if (detail.includes('incorrect username') || detail.includes('password') || detail.includes('250001')) {
      hints.push('This error during SSO usually means browser sign-in did not complete — not that you need a password.')
      hints.push('Re-save Settings with Login method = SSO (password is cleared automatically).')
      hints.push('Complete the browser IdP login on the machine running the backend within 2 minutes.')
    }
    if (idpUrl?.trim() && detail.includes('okta')) {
      hints.push('Confirm the IdP URL matches your Okta Snowflake app integration URL.')
    }
    if (!idpUrl?.trim() && detail.includes('idp')) {
      hints.push('Try adding your IdP URL (Okta/ADFS) in the SSO section, or confirm browser SSO is enabled for your account.')
    }
  }
  if (detail.includes('warehouse') || detail.includes('suspend')) {
    hints.push(`Check that warehouse "${sf.warehouse || 'COMPUTE_WH'}" exists and is not suspended.`)
  }
  if (detail.includes('role') || detail.includes('authorization')) {
    hints.push(`Confirm role "${sf.role || 'SYSADMIN'}" is granted to your user.`)
  }
  if (detail.includes('database') || detail.includes('schema')) {
    hints.push('Verify Database and Schema names exist and your role can access them.')
  }
  if (detail.includes('account')) {
    hints.push('Account identifier format is usually ORG-ACCOUNT or legacy account locator + region.')
  }
  if (!hints.length) {
    hints.push('Review Account, User, login method, and warehouse/role settings in Settings → Snowflake.')
  }
  return uniq(hints)
}

function hintsForService(service, inputs) {
  const name = (service.name || '').toLowerCase()
  const { aws = {}, snowflake = {}, sfAuthMethod = 'password', idpUrl = '' } = inputs

  if (name.includes('snowflake')) {
    return snowflakeHints(service, snowflake, sfAuthMethod, idpUrl)
  }
  if (name.includes('aws') || ['glue', 'stepfunctions', 'logs', 'dynamodb', 'cloudwatch'].some((s) => name.includes(s))) {
    return awsHints(service, aws)
  }
  return ['Review the connection settings for this service and try again.']
}

function formatNotice(service, inputs) {
  const statusLabel = service.status === 'degraded' ? 'degraded' : 'not connected'
  const hints = service.hints?.length
    ? service.hints
    : hintsForService(service, inputs)
  const hintBlock = hints.map((h) => `• ${h}`).join('\n')
  const authNote = service.auth_mode === 'sso'
    ? 'Auth: SSO (no password used)\n\n'
    : service.auth_mode === 'password'
      ? 'Auth: Password\n\n'
      : ''
  const reason = service.detail || service.raw_error || 'Connection test failed.'
  return (
    `${service.name} is ${statusLabel}.\n\n` +
    authNote +
    `Reason: ${reason}\n\n` +
    (service.raw_error && service.raw_error !== reason
      ? `Technical detail: ${service.raw_error}\n\n`
      : '') +
    `What to fix:\n${hintBlock}`
  )
}

export function buildConnectionNotices(conn, inputs = {}) {
  if (!conn) return []

  return conn.services
    .filter((s) => s.status !== 'connected')
    .map((service) => ({
      id: `conn:${service.name}:${service.status}:${service.detail || ''}`,
      agent: 'connectivity',
      text: formatNotice(service, inputs),
    }))
}
