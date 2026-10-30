<script setup>
import Icon from './Icon.vue'
import LossChart from './LossChart.vue'

const props = defineProps({ config: Object, runs: { type: Array, default: () => [] }, finalReport: Object, state: String })
const emit = defineEmits(['refresh'])
const RUN_STATE = { completed: ['已完成', 'tag-ok'], running: ['运行中', 'tag-accent'], interrupted: ['已中断', 'tag-warn'], failed: ['失败', 'tag-error'] }
const pct = value => Number.isFinite(value) ? (value * 100).toFixed(2) + '%' : '—'
const n = value => Number.isFinite(value) ? value.toLocaleString() : '—'
const short = value => value ? (String(value).length > 30 ? String(value).slice(0, 28) + '…' : String(value)) : '—'
const kindName = kind => ({ lr: '逻辑回归', mlp16: 'MLP 16' })[kind] || kind
const runState = run => RUN_STATE[run.state] || [run.state, 'tag-neutral']
const chartHistory = history => (history || []).map(point => ({ ...point, train_loss: point.train_loss ?? point.training_loss }))
</script>

<template>
  <section class="panel">
    <header class="panel-head">
      <div>
        <p class="eyebrow">效果对照</p>
        <h1 id="panel-title" tabindex="-1">分清三种证据</h1>
        <p class="lede">历史验证、一次性最终测试和 Jev 现状是不同性质的结论，不能互相替代，也不能放进同一张排名。</p>
      </div>
      <button type="button" class="btn btn-outline" :disabled="state === 'loading'" @click="emit('refresh')"><Icon name="refresh" />重新读取</button>
    </header>

    <div class="evidence-legend">
      <article class="legend-card"><span class="legend-kicker">A · 历史验证</span><h2>训练运行的验证集</h2><p>每次训练在固定验证集上的结果，用来比较候选；不是最终测试，也不是线上效果。</p></article>
      <article class="legend-card"><span class="legend-kicker">B · 最终测试</span><h2>冻结候选，只评估一次</h2><p>封存测试集只评估一次，结果不再用于挑选模型、调阈值或比较 Jev。</p></article>
      <article class="legend-card legend-pending"><span class="legend-kicker">C · Jev</span><h2>尚未评测</h2><p>没有正式质量评测。判断体验里的单条对照是即时示例，不是评测，不产生排名。</p></article>
    </div>

    <section class="block" aria-labelledby="runs-title">
      <h2 id="runs-title" class="block-title"><span class="kicker">A</span>训练运行与验证结果</h2>
      <div v-if="state === 'loading' && !runs.length" class="skeleton-block card" aria-hidden="true"><div class="sk sk-w40"></div><div class="sk sk-grid"></div></div>
      <div v-else-if="state === 'error'" class="callout callout-error" role="alert"><Icon name="alert" /><span class="callout-body">实验记录读取失败。<button type="button" class="link-btn" @click="emit('refresh')">重新读取</button></span></div>
      <div v-else-if="state === 'locked'" class="state-card compact"><span class="state-icon"><Icon name="lock" /></span><p>载入操作令牌后读取本地实验记录。</p></div>
      <div v-else-if="!runs.length" class="state-card compact"><p>{{ config?.public ? '这个公开部署没有发布实验记录。' : '还没有训练运行；训练在本地 CLI 执行。' }}</p></div>
      <article v-for="run in runs" :key="run.run_id" class="card run-card">
        <header class="run-head">
          <div><h3><code>{{ run.run_id }}</code></h3><p class="fine">数据 <code>{{ short(run.dataset_id) }}</code> · 快照 <code>{{ short(run.snapshot_id) }}</code></p></div>
          <span class="tag" :class="runState(run)[1]">{{ runState(run)[0] }}</span>
        </header>
        <p v-if="run.status_note" class="callout callout-warn"><Icon name="alert" /><span class="callout-body">{{ run.status_note }}</span></p>
        <dl class="inline-stats">
          <div><dt>训练</dt><dd>{{ n(run.training_count) }}</dd></div>
          <div><dt>导入新增</dt><dd>{{ n(run.imported_count) }}</dd></div>
          <div><dt>纠错</dt><dd>{{ n(run.correction_count) }}</dd></div>
          <div><dt>验证</dt><dd>{{ n(run.validation_count) }}</dd></div>
        </dl>
        <div v-if="run.models?.length" class="table-wrap">
          <table class="table table-num">
            <caption class="sr-only">{{ run.run_id }} 的验证集指标</caption>
            <thead><tr><th scope="col">模型</th><th scope="col">spam 查准率</th><th scope="col">spam 召回率</th><th scope="col">macro-F1</th><th scope="col">误拦正常</th><th scope="col">漏检垃圾</th><th scope="col">检查点</th></tr></thead>
            <tbody><tr v-for="model in run.models" :key="model.kind"><th scope="row">{{ model.name || kindName(model.kind) }}</th><td>{{ pct(model.metrics?.precision) }}</td><td>{{ pct(model.metrics?.recall) }}</td><td>{{ pct(model.metrics?.macro_f1) }}</td><td>{{ model.metrics?.false_positive ?? '—' }}</td><td>{{ model.metrics?.false_negative ?? '—' }}</td><td>{{ model.checkpoint_epoch || '—' }}</td></tr></tbody>
          </table>
        </div>
        <p v-else class="fine">这条运行记录没有模型指标。</p>
        <div v-if="run.models?.some(model => model.history?.length)" class="chart-grid">
          <template v-for="model in run.models" :key="model.kind"><LossChart v-if="model.history?.length" :name="model.name || model.kind" :history="chartHistory(model.history)" :checkpoint="model.checkpoint_epoch" /></template>
        </div>
        <p class="fine">{{ run.test_set_evaluated ? '本报告包含最终测试' : '仅含验证结果；最终测试另行记录' }} · 阈值 {{ run.config?.threshold ?? '—' }} · seed {{ run.config?.seed ?? '—' }} · 数值产物复核 {{ run.safe_numeric_reload_matches ? '一致' : '未确认' }}</p>
        <details class="details">
          <summary>来源与说明</summary>
          <p v-if="run.scope" class="fine">范围：{{ run.scope }}</p>
          <p v-if="run.data_audit?.rule" class="fine">数据规则：{{ run.data_audit.rule }}</p>
          <p v-for="model in run.models || []" :key="model.kind" class="fine">{{ kindName(model.kind) }}：误拦 {{ model.metrics?.false_positive ?? '—' }}，漏检 {{ model.metrics?.false_negative ?? '—' }}。报告保留样例 ID、实际标签与分数，不公开私有原文。</p>
          <p class="fine">代码摘要 <code>{{ short(run.code_sha256) }}</code></p>
        </details>
      </article>
    </section>

    <section class="block" aria-labelledby="final-title">
      <h2 id="final-title" class="block-title"><span class="kicker">B</span>独立最终测试</h2>
      <div v-if="finalReport" class="card">
        <div class="metric-row">
          <div class="metric"><span>测试样本</span><b>{{ n(finalReport.metrics?.count) }}</b></div>
          <div class="metric"><span>spam 查准率</span><b>{{ pct(finalReport.metrics?.precision) }}</b></div>
          <div class="metric"><span>spam 召回率</span><b>{{ pct(finalReport.metrics?.recall) }}</b></div>
          <div class="metric"><span>macro-F1</span><b>{{ pct(finalReport.metrics?.macro_f1) }}</b></div>
          <div v-if="Number.isFinite(finalReport.metrics?.false_positive)" class="metric"><span>误拦正常</span><b>{{ finalReport.metrics.false_positive }}</b></div>
          <div v-if="Number.isFinite(finalReport.metrics?.false_negative)" class="metric"><span>漏检垃圾</span><b>{{ finalReport.metrics.false_negative }}</b></div>
        </div>
        <p class="fine">冻结候选 <code>{{ finalReport.candidate?.run_id }}</code> · {{ kindName(finalReport.candidate?.kind) }} · 阈值 {{ finalReport.candidate?.threshold }}<template v-if="finalReport.completed_at"> · {{ finalReport.completed_at }}</template></p>
        <p v-if="finalReport.scope" class="fine">范围：{{ finalReport.scope }}</p>
        <p class="note-strong"><Icon name="lock" />只评估一次；不再用于选择模型、调阈值或比较 Jev。</p>
      </div>
      <div v-else-if="state === 'locked'" class="state-card compact"><span class="state-icon"><Icon name="lock" /></span><p>载入操作令牌后读取。</p></div>
      <div v-else class="state-card compact"><p>{{ state === 'loading' ? '正在读取…' : '没有可展示的最终测试报告。' }}</p></div>
    </section>

    <section class="block" aria-labelledby="jev-title">
      <h2 id="jev-title" class="block-title"><span class="kicker">C</span>Jev 对照现状</h2>
      <div class="card jev-status">
        <div class="jev-status-main">
          <span class="tag tag-neutral">尚未评测</span>
          <p>目前没有 Jev 在这个任务上的正式质量、时延或成本结论。项目里 4 条 AI 自编烟测样本只用于功能检查，不是质量证据。</p>
        </div>
        <ul class="check-list">
          <li><Icon name="check" />固定官方端点与模型版本 <code>{{ config?.decision?.model_version || 'jev-1.14.0' }}</code></li>
          <li><Icon name="check" />响应契约校验、字段白名单与错误脱敏</li>
          <li><Icon name="check" />发送前 TLS 检查、逐次外发同意、无自动重试</li>
          <li><Icon name="info" />以上是模拟上游下的软件测试结论，不代表判断质量</li>
        </ul>
        <p class="fine">新的评测需要另立数据版本、独立人工标注、预先冻结配置并单独授权付费请求；不使用已开封的旧测试集。</p>
      </div>
    </section>
  </section>
</template>
