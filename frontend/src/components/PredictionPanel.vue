<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { labelName, percentage, request } from '../api.js'

const models = ref([])
const modelsLoading = ref(false)
const modelsError = ref('')
const text = ref('')
const model = ref('mlp16')
const threshold = ref(0.5)
const busy = ref(false)
const error = ref('')
const result = ref(null)
let predictionId = 0
let controller
const validThreshold = computed(() => threshold.value !== '' && Number.isFinite(Number(threshold.value)) && Number(threshold.value) >= 0 && Number(threshold.value) <= 1)
const canPredict = computed(() => text.value.trim().length > 0 && text.value.length <= 2000 && validThreshold.value && models.value.length > 0 && !busy.value)
const selectedModel = computed(() => models.value.find(item => item.id === model.value))

async function loadModels() {
  modelsLoading.value = true
  modelsError.value = ''
  try {
    models.value = await request('/models')
    if (!models.value.some(item => item.id === model.value)) model.value = models.value[0]?.id || ''
  } catch (cause) {
    modelsError.value = cause.message
  } finally {
    modelsLoading.value = false
  }
}

watch([text, model, threshold], () => {
  predictionId += 1
  controller?.abort()
  busy.value = false
  result.value = null
  error.value = ''
})

async function predict() {
  if (!canPredict.value) return
  const current = ++predictionId
  controller?.abort()
  controller = new AbortController()
  busy.value = true
  error.value = ''
  result.value = null
  try {
    const response = await request('/predict', {
      method: 'POST', signal: controller.signal,
      body: { text: text.value, model: model.value, threshold: Number(threshold.value) },
    })
    if (current === predictionId) result.value = response
  } catch (cause) {
    if (current === predictionId && cause.name !== 'AbortError') error.value = cause.message
  } finally {
    if (current === predictionId) busy.value = false
  }
}

onMounted(loadModels)
onBeforeUnmount(() => controller?.abort())
</script>

<template>
  <section aria-labelledby="prediction-heading">
    <div class="page-heading">
      <div><p class="eyebrow">PREDICTION</p><h1 id="prediction-heading">让模型读一条短信</h1><p>选择模型和阈值，观察同一段文本如何被分类。</p></div>
      <span class="quiet-tag">实时推断</span>
    </div>

    <div class="prediction-layout">
      <form class="card prediction-form" @submit.prevent="predict">
        <div class="card-heading"><h2>输入与设置</h2><span class="muted small">单条文本</span></div>
        <div v-if="modelsError" class="notice error" role="alert">{{ modelsError }} <button class="text-button" type="button" :disabled="modelsLoading" @click="loadModels">重新加载模型</button></div>
        <div class="field">
          <label for="prediction-text">短信文本 <span class="required">*</span></label>
          <textarea id="prediction-text" v-model="text" maxlength="2000" rows="8" placeholder="在这里输入要判断的短信内容…" aria-describedby="text-help" required></textarea>
          <div id="text-help" class="field-meta"><span>当前模型使用英文短信训练，中文效果尚未验证。</span><span>{{ text.length }} / 2000</span></div>
        </div>
        <div class="form-grid">
          <div class="field"><label for="prediction-model">分类模型</label><select id="prediction-model" v-model="model" :disabled="modelsLoading || !models.length"><option v-if="!models.length" value="mlp16">{{ modelsLoading ? '正在加载模型…' : '暂无可用模型' }}</option><option v-for="item in models" :key="item.id" :value="item.id">{{ item.name }}</option></select><p class="field-help">使用已训练并保存的模型。</p></div>
          <div class="field"><label for="prediction-threshold">分类阈值</label><input id="prediction-threshold" v-model="threshold" type="number" min="0" max="1" step="0.05" required /><p class="field-help">得分 ≥ 阈值时，判为垃圾短信。</p></div>
        </div>
        <p v-if="!validThreshold" class="inline-error" role="alert">阈值必须是0到1之间的数字。</p>
        <div v-if="error" class="notice error" role="alert">{{ error }}</div>
        <div class="form-actions"><span class="muted small">修改输入后，需重新预测。</span><button class="button primary" type="submit" :disabled="!canPredict"><span v-if="busy" class="spinner" aria-hidden="true"></span>{{ busy ? '正在预测…' : '开始预测' }}<span v-if="!busy" aria-hidden="true">↗</span></button></div>
      </form>

      <aside class="card result-card" aria-labelledby="prediction-result-heading" :aria-busy="busy">
        <div class="card-heading"><h2 id="prediction-result-heading">预测结果</h2><span class="status-dot" :class="{ ready: result }" aria-hidden="true"></span></div>
        <div v-if="result" class="prediction-result" role="status" aria-live="polite">
          <span :class="['result-label', result.label]">{{ labelName(result.label) }}</span>
          <p class="score-label">垃圾短信得分</p><p class="score-value">{{ percentage(result.spam_score) }}</p>
          <div class="score-track" aria-hidden="true"><span :style="{ width: `${result.spam_score * 100}%` }"></span><i :style="{ left: `${result.threshold * 100}%` }"></i></div>
          <div class="score-scale"><span>0</span><span>阈值 {{ result.threshold }}</span><span>1</span></div>
          <dl class="result-details"><div><dt>使用模型</dt><dd>{{ models.find(item => item.id === result.model)?.name || result.model }}</dd></div><div><dt>判定规则</dt><dd>得分 {{ result.spam_score >= result.threshold ? '≥' : '<' }} {{ result.threshold }}</dd></div></dl>
          <p class="result-footnote">得分表示模型的判断倾向，不等于结果正确率。</p>
        </div>
        <div v-else class="empty-state prediction-empty" role="status" aria-live="polite">
          <span v-if="busy" class="large-spinner spinner" aria-hidden="true"></span>
          <svg v-else class="empty-icon" viewBox="0 0 48 48" fill="none" aria-hidden="true"><rect x="9" y="7" width="30" height="35" rx="5" stroke="currentColor" stroke-width="1.8"/><path d="M17 17h14M17 24h14M17 31h8" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>
          <h3>{{ busy ? '模型正在计算' : '等待一次预测' }}</h3><p>{{ busy ? '结果返回后将显示在这里。' : '输入短信并点击“开始预测”，查看分类与得分。' }}</p>
        </div>
        <div class="card-note"><strong>观察重点</strong><p>提高阈值通常会减少垃圾判定，同时可能漏掉更多垃圾短信。</p><span v-if="selectedModel" class="model-reference">当前选择：{{ selectedModel.name }}</span></div>
      </aside>
    </div>
  </section>
</template>
