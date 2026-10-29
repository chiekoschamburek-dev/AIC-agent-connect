import { useEffect, useRef } from 'react'
import type { UIDomain, UIState } from './engine'
import { CHARTER_ROLE_LABEL, type VizEvent } from './types'

function agentName(state: UIState, ref?: string): string {
  if (!ref) return '—'
  return state.agents[ref]?.name ?? ref
}

function shortAic(aic?: string): string {
  const value = String(aic ?? '—')
  if (value.length <= 34) return value
  return `${value.slice(0, 16)}…${value.slice(-8)}`
}

function linkedEventLine(state: UIState, e: VizEvent): { icon: string; text: string } | null {
  if (e.type === 'token') {
    return {
      icon: e.kind === 'grant' ? '📜' : '🎫',
      text: `${e.kind === 'grant' ? '域级 Grant' : '派生 Token'} · ${agentName(state, e.issuer)} → ${agentName(state, e.holder)}${e.label ? `（${e.label}）` : ''}`,
    }
  }
  if (e.type === 'pep') {
    return {
      icon: e.decision === 'allow' ? '🟢' : '⛔',
      text: `PEP ${e.decision === 'allow' ? '放行' : '拒绝'} · ${agentName(state, e.caller)} → ${agentName(state, e.target)}（${e.rule}）`,
    }
  }
  if (e.type === 'reconcile') {
    return { icon: '🧾', text: `对账 · ${e.totalUses} 次调用 · 双方签名` }
  }
  return null
}

export function DomainDrawer({
  domain,
  state,
  onClose,
}: {
  domain: UIDomain | null
  state: UIState
  onClose: () => void
}) {
  const closeRef = useRef<HTMLButtonElement>(null)
  const onCloseRef = useRef(onClose)
  useEffect(() => {
    onCloseRef.current = onClose
  }, [onClose])

  const open = Boolean(domain)
  useEffect(() => {
    if (!open) return
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    closeRef.current?.focus()
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onCloseRef.current()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => {
      document.body.style.overflow = previousOverflow
      window.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

  if (!domain) return null
  const charter = domain.charter
  const linked = state.ledger
    .filter((e) => (e.type === 'token' || e.type === 'pep' || e.type === 'reconcile')
      && e.domain === domain.id)
    .map((e) => ({ e, line: linkedEventLine(state, e), t: e.t }))
    .filter((x) => x.line !== null)

  return (
    <div
      className="drawer-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose()
      }}
    >
      <aside className="domain-drawer" role="dialog" aria-modal="true" aria-labelledby="domain-drawer-title">
        <header className="domain-drawer-head">
          <div className="domain-drawer-title-wrap">
            <div className="domain-kicker">协作域章程</div>
            <h2 id="domain-drawer-title">{charter?.name ?? domain.id}</h2>
            <div className="domain-sub">
              {domain.id} · revision r{domain.revision}
            </div>
          </div>
          <div className="domain-drawer-actions">
            <span className={`treaty-status ${domain.status === 'frozen' ? 'frozen' : 'active'}`}>
              {domain.status === 'frozen' ? '已冻结' : '活跃'}
            </span>
            <button ref={closeRef} className="domain-close" aria-label="关闭章程" onClick={onClose}>
              ×
            </button>
          </div>
        </header>

        <div className="domain-body">
          <div className="domain-meta">
            <div><span>章程哈希</span><code>{charter?.hash ?? '未提供'}</code></div>
            <div><span>有效期</span><b>{charter?.window ?? '未提供'}</b></div>
            <div><span>双签</span><b>{(charter?.signatures ?? []).filter((s) => s.verified).length} / {(charter?.signatures ?? []).length || 0}</b></div>
            <div><span>对账</span><b>{domain.reconciledUses !== undefined ? `${domain.reconciledUses} 次调用` : '尚未对账'}</b></div>
          </div>

          {!charter ? (
            <div className="domain-empty">
              该 run 的协作域事件只带 <code>domainId / revision</code>，没有上传章程明细。
              旧录制数据仍显示摘要卡；新录制/内置剧本会带完整章程。
            </div>
          ) : (
            <>
              <section className="domain-section">
                <h3>双签验证</h3>
                {(charter.signatures ?? []).length === 0 && (
                  <div className="domain-empty">未提供签名清单</div>
                )}
                <div className="signature-list">
                  {(charter.signatures ?? []).map((sig, index) => (
                    <div key={`${sig.signer ?? sig.name ?? 'sig'}-${index}`} className="signature-row">
                      <span className={sig.verified ? 'signature-ok' : 'signature-bad'}>
                        {sig.verified ? '✓' : '✗'}
                      </span>
                      <span className="signature-name">{sig.name ?? sig.signer ?? sig.aic ?? '未知签名人'}</span>
                      <span className="signature-role">{CHARTER_ROLE_LABEL[sig.role ?? ''] ?? sig.role ?? '成员'}</span>
                      <code className="signature-aic">{shortAic(sig.aic)}</code>
                    </div>
                  ))}
                </div>
              </section>

              <section className="domain-section">
                <h3>成员 <span>{charter.members?.length ?? 0}</span></h3>
                <div className="charter-list">
                  {(charter.members ?? []).map((member) => (
                    <div key={`${member.aic}-${member.role}`} className="charter-row">
                      <span className={`charter-role role-${member.role}`}>
                        {CHARTER_ROLE_LABEL[member.role] ?? member.role}
                      </span>
                      <span className="charter-name">{member.name ?? member.aic}</span>
                      <code className="charter-aic">{shortAic(member.aic)}</code>
                    </div>
                  ))}
                </div>
              </section>

              <section className="domain-section">
                <h3>域资产 <span>{charter.assets?.length ?? 0}</span></h3>
                <div className="charter-list">
                  {(charter.assets ?? []).map((asset, index) => (
                    <div key={`${asset.skill}-${index}`} className="charter-row asset-row">
                      <code className="asset-skill">{asset.skill}</code>
                      <span className="charter-name">{asset.provider ?? asset.providerAic ?? '—'}</span>
                      <span className="asset-allowed">
                        允许：{asset.allowedRoles?.map((r) => CHARTER_ROLE_LABEL[r] ?? r).join(' / ')
                          ?? asset.allowedMembers?.join(' / ') ?? '—'}
                      </span>
                    </div>
                  ))}
                </div>
              </section>

              {(charter.issuers ?? []).length > 0 && (
                <section className="domain-section">
                  <h3>签发人</h3>
                  <div className="issuer-list">
                    {(charter.issuers ?? []).map((issuer) => (
                      <span key={issuer} className="issuer-chip">{issuer}</span>
                    ))}
                  </div>
                </section>
              )}
            </>
          )}

          <section className="domain-section">
            <h3>域内授权链</h3>
            {linked.length === 0 ? (
              <div className="domain-empty">本域暂无 Grant / Token / PEP / 对账事件</div>
            ) : (
              <div className="domain-link-list">
                {linked.map(({ e, line, t }) => (
                  <div key={`${e.type}-${t}-${line?.text}`} className="domain-link-row">
                    <span className="domain-link-t">{t.toFixed(1)}s</span>
                    <span className="domain-link-icon">{line?.icon}</span>
                    <span>{line?.text}</span>
                  </div>
                ))}
              </div>
            )}
          </section>
        </div>
      </aside>
    </div>
  )
}
