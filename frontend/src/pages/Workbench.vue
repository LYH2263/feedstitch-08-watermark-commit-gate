<script setup>
import { onMounted, ref } from 'vue'
import { getJSON, postJSON } from '../api'

const sources = ref([])
const sourceId = ref(null)
const mode = ref('incremental')
const last = ref(null)
const history = ref([])
const err = ref('')
const busy = ref(false)

function statusClass(s) {
  if (s === 'ok') return 'ok'
  if (s === 'gate_failed' || s === 'running') return 'warn'
  return 'danger'
}

async function refresh() {
  history.value = await getJSON('/api/runs')
}

async function runOnce() {
  err.value = ''
  busy.value = true
  try {
    last.value = await postJSON(`/api/sources/${sourceId.value}/runs`, { mode: mode.value })
    await refresh()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

onMounted(async () => {
  sources.value = await getJSON('/api/sources')
  if (sources.value.length) sourceId.value = sources.value[0].id
  await refresh()
})
</script>
<template>
  <div class="page">
    <h1>运行台</h1>
    <p class="muted">选源触发增量或全量；提交闸核对通过后水位才会推进，报告数字与 Staging / 冲突 / Canonical 列表页同源。</p>
    <div class="panel">
      <label>
        源
        <select v-model.number="sourceId">
          <option v-for="s in sources" :key="s.id" :value="s.id">{{ s.id }}. {{ s.name }}</option>
        </select>
      </label>
      <label style="margin-left:1rem">
        模式
        <select v-model="mode">
          <option value="incremental">incremental</option>
          <option value="full">full</option>
        </select>
      </label>
      <button style="margin-left:1rem" :disabled="busy || !sourceId" @click="runOnce">
        {{ busy ? '运行中…' : '执行 Run' }}
      </button>
    </div>
    <p v-if="err" class="tag danger">{{ err }}</p>
    <div class="panel" v-if="last">
      <h3>最近一次报告</h3>
      <p>
        Run #{{ last.id }} ·
        <span class="tag" :class="statusClass(last.status)">{{ last.status }}</span> ·
        parsed {{ last.parsed_count }} /
        upserted {{ last.upserted_count }} /
        conflict {{ last.conflict_count }} /
        skipped {{ last.skipped_count }}
      </p>
      <p class="mono muted">
        canonical {{ last.report?.pre_run_canonical_count }} → {{ last.report?.canonical_count }}
        · 水位 {{ last.report?.pre_run_watermark_ts || '—' }} ⇒ 候选 {{ last.report?.candidate_watermark_ts || '—' }}
        · 已提交 {{ last.report?.committed_watermark_ts || '—' }}
      </p>
      <div v-if="last.report?.gate">
        <p>
          提交闸
          <span class="tag" :class="last.report.gate.passed ? 'ok' : 'danger'">
            {{ last.report.gate.passed ? '通过 · 水位已提交' : '未通过 · 水位保持跑前值' }}
          </span>
          <span class="muted mono">{{ last.report.gate.summary }}</span>
        </p>
        <table>
          <thead>
            <tr><th>闸检查</th><th>期望</th><th>实际</th><th>结果</th></tr>
          </thead>
          <tbody>
            <tr v-for="c in last.report.gate.checks" :key="c.name">
              <td class="mono">{{ c.name }}</td>
              <td class="mono">{{ c.expected }}</td>
              <td class="mono">{{ c.actual }}</td>
              <td><span class="tag" :class="c.ok ? 'ok' : 'danger'">{{ c.ok ? 'ok' : 'fail' }}</span></td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-if="last.report?.fault_injection" class="tag warn">故障注入：{{ last.report.fault_injection }}</p>
      <p class="muted">
        对照列表页同源数字：
        <router-link to="/staging">Staging</router-link> ·
        <router-link to="/conflicts">冲突队列</router-link> ·
        <router-link to="/canonical">Canonical</router-link> ·
        <router-link to="/watermarks">水位线</router-link>
      </p>
    </div>
    <div class="panel">
      <h3>历史 Runs</h3>
      <table>
        <thead>
          <tr>
            <th>ID</th><th>源</th><th>模式</th><th>状态</th><th>闸</th><th>parsed</th><th>upserted</th><th>conflict</th><th>skipped</th><th>时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in history" :key="r.id">
            <td>{{ r.id }}</td>
            <td>{{ r.source_name }}</td>
            <td>{{ r.mode }}</td>
            <td><span class="tag" :class="statusClass(r.status)">{{ r.status }}</span></td>
            <td>
              <span v-if="r.report?.gate" class="tag" :class="r.report.gate.passed ? 'ok' : 'danger'">
                {{ r.report.gate.passed ? '通过' : '未通过' }}
              </span>
              <span v-else class="muted">—</span>
            </td>
            <td>{{ r.parsed_count }}</td>
            <td>{{ r.upserted_count }}</td>
            <td>{{ r.conflict_count }}</td>
            <td>{{ r.skipped_count }}</td>
            <td class="mono">{{ r.created_at }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
