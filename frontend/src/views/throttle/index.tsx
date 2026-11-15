import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'
import { useModuleRows } from '@/api/useModuleRows'

const ENDPOINT = '/api/throttle'
const columns = [
  '规则编号',
  '目标域组',
  '初始速率',
  '最大速率',
  '当前速率',
  '降档版本',
  '恢复引用报告',
  '错误率阈值',
  '降速恢复间隔'
]
// 状态机只允许单向推进：每个状态只暴露合法的下一步，从按钮层堵住回退与跳级。
const NEXT_ACTIONS: Record<string, string[]> = {
  草稿: ['提交规则评审', '关闭规则'],
  规则评审中: ['启用规则', '关闭规则'],
  已启用: ['限速降档', '恢复速率', '关闭规则'],
  已关闭: []
}

type Row = Record<string, string | number | null>
type ReportRow = Record<string, string | number | null>
type ArtifactRow = Record<string, string | number | null>

export default function ThrottleView() {
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [eligibleReports, setEligibleReports] = useState<ReportRow[]>([])
  const [artifacts, setArtifacts] = useState<ArtifactRow[]>([])
  const [reportPick, setReportPick] = useState<Record<string, string>>({})
  const [downgradeRate, setDowngradeRate] = useState<Record<string, string>>({})
  const [queueLen, setQueueLen] = useState<Record<string, string>>({})
  const filterFields = useMemo(() => ['规则编号', '目标域组', '初始速率'], [])

  const { rows, total, errorMessage, infoMessage, reload, runAction, setErrorMessage, setInfoMessage } =
    useModuleRows<Row>({ endpoint: ENDPOINT, filters, listErrorMessage: '限速规则列表读取失败' })

  const loadSidePanels = useCallback(async () => {
    try {
      const [reportResp, artifactResp] = await Promise.all([
        request(`${ENDPOINT}/eligible-reports`),
        request(`${ENDPOINT}/recovery/artifacts`),
      ])
      if (reportResp.ok) {
        const payload = await reportResp.json()
        setEligibleReports(payload.items ?? [])
      }
      if (artifactResp.ok) {
        const payload = await artifactResp.json()
        setArtifacts(payload.items ?? [])
      }
    } catch {
      // 侧栏加载失败不阻塞主表，页脚给出提示。
      setErrorMessage('健康报告或恢复凭证读取失败，请刷新重试')
    }
  }, [setErrorMessage])

  useEffect(() => {
    void loadSidePanels()
  }, [loadSidePanels, rows])

  async function handleAction(action: string, row: Row) {
    const values: Record<string, unknown> = { action }
    if (action === '启用规则' || action === '恢复速率') {
      const reportId = reportPick[String(row.id)]
      if (!reportId) {
        setErrorMessage(`${action}前必须选择一份已确认且覆盖当前窗口、错误队列已清零的健康报告`)
        return
      }
      values.健康报告id = reportId
    }
    if (action === '限速降档') {
      const rate = downgradeRate[String(row.id)]
      if (rate === undefined || rate.trim() === '') {
        setErrorMessage('限速降档必须填写降档速率（需低于当前速率）')
        return
      }
      values.降档速率 = rate
      values.错误队列长度 = queueLen[String(row.id)] ?? ''
    }
    await runAction(`${ENDPOINT}/${row.id}/actions`, { values }, '动态限速动作未生效，请稍后重试')
    void loadSidePanels()
  }

  async function submitRecovery(channel: 'bookmark' | 'import' | 'upload', row: Row) {
    const reportId = reportPick[String(row.id)]
    if (!reportId) {
      setErrorMessage('恢复速率前必须选择一份已确认且覆盖当前窗口的健康报告')
      return
    }
    const report = eligibleReports.find((item) => String(item.id) === String(reportId))
    const payload = {
      规则编号: row.规则编号,
      健康报告编号: report?.报告编号,
    }
    try {
      if (channel === 'upload') {
        const fileText = JSON.stringify(payload, null, 2)
        const response = await request(`${ENDPOINT}/recovery/upload`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: fileText,
        })
        const data = await response.json()
        if (data.ok === false) setErrorMessage(data.message)
        else setInfoMessage(data.message ?? '上传恢复已受理')
      } else {
        const path = channel === 'bookmark' ? '/recovery/bookmark' : '/recovery/import'
        await runAction(`${ENDPOINT}${path}`, { values: payload }, '恢复凭证提交失败')
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '恢复凭证提交失败')
    }
    await reload()
    void loadSidePanels()
  }

  async function replayArtifact(artifactId: string | number | null) {
    if (artifactId === null) return
    await runAction(
      `${ENDPOINT}/recovery/artifacts/${artifactId}/replay`,
      {},
      '凭证回读失败',
    )
    void loadSidePanels()
  }

  return (
    <section className="page" data-module="throttle">
      <header className="page-head">
        <div>
          <h2>动态限速管理</h2>
          <p className="page-desc">
            规则只能按 草稿→规则评审中→已启用→已关闭 单向推进；恢复速率必须引用已确认、覆盖当前窗口且错误队列已清零的健康报告。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('限速规则登记入口尚未接入审批流')}>
            登记限速规则
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出动态限速清单
          </button>
        </div>
      </header>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            <th>状态</th>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>健康报告引用 / 降档参数</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const rowStatus = String(row.status ?? '')
            const allowed = NEXT_ACTIONS[rowStatus] ?? []
            const needReport = allowed.includes('启用规则') || allowed.includes('恢复速率')
            return (
              <tr key={String(row.id)}>
                <td><span className={`status-tag status-${rowStatus}`}>{rowStatus}</span></td>
                {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
                <td>
                  {needReport && (
                    <label className="inline-field">
                      <span>健康报告</span>
                      <select
                        value={reportPick[String(row.id)] ?? ''}
                        onChange={(event) => setReportPick((prev) => ({ ...prev, [String(row.id)]: event.target.value }))}
                      >
                        <option value="">请选择已确认报告</option>
                        {eligibleReports.map((report) => (
                          <option key={String(report.id)} value={String(report.id)}>
                            {String(report.报告编号)}（检查 {String(report.检查时间戳)}，错误队列 {String(report.错误队列长度)}）
                          </option>
                        ))}
                      </select>
                    </label>
                  )}
                  {allowed.includes('限速降档') && (
                    <div className="inline-fields">
                      <label className="inline-field">
                        <span>降档速率</span>
                        <input
                          value={downgradeRate[String(row.id)] ?? ''}
                          placeholder="低于当前速率"
                          onChange={(event) => setDowngradeRate((prev) => ({ ...prev, [String(row.id)]: event.target.value }))}
                        />
                      </label>
                      <label className="inline-field">
                        <span>错误队列长度</span>
                        <input
                          value={queueLen[String(row.id)] ?? ''}
                          placeholder="降档时快照"
                          onChange={(event) => setQueueLen((prev) => ({ ...prev, [String(row.id)]: event.target.value }))}
                        />
                      </label>
                    </div>
                  )}
                </td>
                <td className="row-actions">
                  {allowed.map((action) => (
                    <button key={action} className="link" type="button" onClick={() => void handleAction(action, row)}>
                      {action}
                    </button>
                  ))}
                  {allowed.includes('恢复速率') && (
                    <>
                      <button className="link" type="button" onClick={() => void submitRecovery('bookmark', row)}>
                        书签恢复
                      </button>
                      <button className="link" type="button" onClick={() => void submitRecovery('import', row)}>
                        导入恢复
                      </button>
                      <button className="link" type="button" onClick={() => void submitRecovery('upload', row)}>
                        上传恢复
                      </button>
                    </>
                  )}
                  {allowed.length === 0 && <span className="muted">已关闭，无可用动作</span>}
                </td>
              </tr>
            )
          })}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 3}>暂无动态限速数据，可先登记限速规则</td>
            </tr>
          )}
        </tbody>
      </table>

      <section className="side-panel">
        <h3>当前可引用的已确认健康报告</h3>
        <p className="page-desc">仅列出已确认、采样窗口覆盖当前时间、指标完整且错误队列已清零的报告；窗口冲突时后端只放行检查时间最早的一份。</p>
        <table className="data-table compact">
          <thead>
            <tr><th>报告编号</th><th>检查时间戳</th><th>采样窗口</th><th>CPU峰值</th><th>错误队列长度</th></tr>
          </thead>
          <tbody>
            {eligibleReports.map((report) => (
              <tr key={String(report.id)}>
                <td>{String(report.报告编号)}</td>
                <td>{String(report.检查时间戳)}</td>
                <td>{String(report.采样窗口开始)} ~ {String(report.采样窗口结束)}</td>
                <td>{String(report.CPU峰值)}</td>
                <td>{String(report.错误队列长度)}</td>
              </tr>
            ))}
            {eligibleReports.length === 0 && (
              <tr><td className="empty-state" colSpan={5}>暂无可用报告，请先在采集器健康页确认当前窗口的报告</td></tr>
            )}
          </tbody>
        </table>
      </section>

      <section className="side-panel">
        <h3>恢复凭证（书签 / 上传 / 导入，原始版本留存）</h3>
        <table className="data-table compact">
          <thead>
            <tr><th>凭证编号</th><th>通道</th><th>规则</th><th>版本</th><th>处理结果</th><th>处理后速率</th><th>提交时间</th><th>操作</th></tr>
          </thead>
          <tbody>
            {artifacts.map((artifact) => (
              <tr key={String(artifact.id)}>
                <td>{String(artifact.凭证编号)}</td>
                <td>{String(artifact.通道)}</td>
                <td>{String(artifact.规则编号)}</td>
                <td>v{String(artifact.版本)}</td>
                <td>{String(artifact.处理结果)}</td>
                <td>{String(artifact.处理后速率)}</td>
                <td>{String(artifact.提交时间)}</td>
                <td>
                  <button className="link" type="button" onClick={() => void replayArtifact(artifact.id as number)}>
                    回读重放
                  </button>
                </td>
              </tr>
            ))}
            {artifacts.length === 0 && (
              <tr><td className="empty-state" colSpan={8}>暂无恢复凭证</td></tr>
            )}
          </tbody>
        </table>
      </section>

      <footer className="page-foot">
        <span>共 {total} 条动态限速记录</span>
        {infoMessage && <span className="info-text">{infoMessage}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
