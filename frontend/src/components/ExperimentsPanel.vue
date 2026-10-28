<script setup>
import { onMounted, ref } from 'vue'
import { number, percentage, request } from '../api.js'
import LossChart from './LossChart.vue'

const loading = ref(false)
const error = ref('')
const experiment = ref(null)

async function loadExperiment() {
  if (loading.value) return
  loading.value = true
  error.value = ''
  try {
    experiment.value = await request('/experiments')
  } catch (cause) {
    error.value = cause.message
  } finally {
    loading.value = false
  }
}

onMounted(loadExperiment)
</script>

<template>
  <section aria-labelledby="experiments-heading">
    <div class="page-heading"><div><p class="eyebrow">EXPERIMENTS</p><h1 id="experiments-heading">用证据比较模型</h1><p>把模型结构、分类表现和训练过程放在一起判断。</p></div><button class="button secondary" type="button" :disabled="loading" @click="loadExperiment">{{ loading ? '正在加载…' : '刷新实验' }}</button></div>
    <div v-if="error" class="notice error" role="alert">{{ error }}<button class="text-button" type="button" :disabled="loading" @click="loadExperiment">重试读取实验</button></div>
    <div v-if="loading && !experiment" class="card empty-state experiment-loading" role="status"><span class="spinner" aria-hidden="true"></span><p>正在读取实验记录…</p></div>

    <template v-if="experiment">
      <div class="notice info experiment-boundary"><span class="notice-symbol" aria-hidden="true">i</span><div><strong>{{ experiment.evaluation_split === 'validation' ? '当前展示验证集结果。' : '当前评估集合：' + experiment.evaluation_split }}</strong><span>{{ experiment.test_set_evaluated ? '测试集已评估。' : '测试集尚未评估。' }}{{ experiment.near_duplicate_checked ? '近重复检查已完成。' : '近重复文本仍待检查。' }}</span></div></div>
      <div class="experiment-summary"><div class="summary-cell"><span>评估样本</span><strong>{{ number(experiment.sample_count) }}<small>条</small></strong></div><div class="summary-cell"><span>正常短信</span><strong>{{ number(experiment.class_counts?.ham) }}<small>条</small></strong></div><div class="summary-cell"><span>垃圾短信</span><strong>{{ number(experiment.class_counts?.spam) }}<small>条</small></strong></div><div class="summary-cell"><span>统一分类阈值</span><strong>{{ Number.isFinite(experiment.threshold) ? experiment.threshold : '—' }}</strong></div></div>

      <section class="card model-comparison" aria-labelledby="model-comparison-heading">
        <div class="card-heading"><div><h2 id="model-comparison-heading">模型表现</h2><p class="section-description">同一评估集合、同一分类阈值下的实际记录。</p></div><span class="quiet-tag compact">{{ experiment.evaluation_split === 'validation' ? '验证集' : experiment.evaluation_split }}</span></div>
        <div class="table-scroll"><table class="comparison-table"><thead><tr><th scope="col">模型</th><th scope="col">参数量</th><th scope="col">保留轮次</th><th scope="col">准确率</th><th scope="col">查准率</th><th scope="col">召回率</th><th scope="col">宏平均 F1</th><th scope="col">抓到垃圾</th><th scope="col">误判正常</th><th scope="col">漏掉垃圾</th><th scope="col">正常判对</th></tr></thead><tbody><tr v-for="model in experiment.models" :key="model.id"><th scope="row"><span class="model-name">{{ model.name }}</span><span class="model-id">{{ model.id }}</span></th><td>{{ number(model.parameter_count) }}</td><td>{{ Number.isFinite(model.checkpoint_epoch) ? `第${model.checkpoint_epoch}轮` : '—' }}</td><td>{{ percentage(model.metrics?.accuracy) }}</td><td class="metric-emphasis">{{ percentage(model.metrics?.precision) }}</td><td class="metric-emphasis">{{ percentage(model.metrics?.recall) }}</td><td>{{ percentage(model.metrics?.macro_f1) }}</td><td>{{ number(model.metrics?.true_positive) }}</td><td>{{ number(model.metrics?.false_positive) }}</td><td>{{ number(model.metrics?.false_negative) }}</td><td>{{ number(model.metrics?.true_negative) }}</td></tr></tbody></table></div>
        <div class="table-explainer"><p><strong>查准率</strong>：判成垃圾的短信中，有多少确实是垃圾。</p><p><strong>召回率</strong>：实际垃圾短信中，有多少被识别出来。</p></div>
      </section>

      <div class="section-heading"><div><h2>训练过程</h2><p>同时观察训练与验证损失，判断模型是否开始过拟合。</p></div></div>
      <div class="training-grid"><section v-for="model in experiment.models.filter(item => item.id !== 'lr')" :key="model.id" class="card training-card"><div class="card-heading"><h3>{{ model.name }}</h3><span class="muted small">{{ number(model.parameter_count) }} 参数</span></div><LossChart :name="model.name" :history="model.history || []" :checkpoint="model.checkpoint_epoch" /></section></div>
      <p v-if="experiment.models.some(model => model.id === 'lr' && !model.history?.length)" class="baseline-note">逻辑回归基线未记录逐轮损失，因此不展示训练曲线。</p>
      <section v-if="experiment.notes?.length" class="evidence-notes" aria-labelledby="experiment-notes-heading"><h3 id="experiment-notes-heading">结果的使用边界</h3><ul><li v-for="note in experiment.notes" :key="note">{{ note }}</li></ul></section>
    </template>
  </section>
</template>
