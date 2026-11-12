<script setup>
import { onMounted, ref } from 'vue'
import { getJSON, patchJSON, putJSON } from '../api'

const marks = ref([])
const settings = ref([])
const err = ref('')
const msg = ref('')

function gateTag(m) {
  const g = m.last_gate
  if (!g) return null
  if (g.passed === true) return { cls: 'ok', text: '闸通过' }
  if (g.status === 'gate_failed') return { cls: 'warn', text: '闸未通过 · 水位未提交' }
  return { cls: 'danger', text: g.status }
}

async function refresh() {
  marks.value = await getJSON('/api/watermarks')
  settings.value = await getJSON('/api/settings')
}

async function saveLookback(row) {
  err.value = ''
  msg.value = ''
  try {
    await patchJSON(`/api/sources/${row.source_id}/lookback`, {
      lookback_seconds: Number(row.lookback_seconds) || 0,
    })
    msg.value = `已写入源 ${row.source_id} 的 lookback_seconds（底座 ingest 暂忽略此字段，留给 0-1）`
    await refresh()
  } catch (e) {
    err.value = String(e.message || e)
  }
}

async function saveSetting(row) {
  await putJSON(`/api/settings/${encodeURIComponent(row.key)}`, { value: row.value })
  msg.value = `设置 ${row.key} 已保存`
  await refresh()
}

onMounted(async () => {
  try {
    await refresh()
  } catch (e) {
    err.value = String(e.message || e)
  }
})
</script>
<template>
  <div class="page">
    <h1>水位线与设置</h1>
    <p class="muted">
      水位只在提交闸对账通过后推进；本页“当前水位”应与“最近成功提交”一致。
      演示闸失败：把设置 <span class="mono">gate_fault_injection</span> 改为
      <span class="mono">drop_staging_row</span> / <span class="mono">skew_report_counter</span> /
      <span class="mono">canonical_drift</span> 后去运行台触发 Run（改回 <span class="mono">off</span> 恢复）。
    </p>
    <p v-if="err" class="tag danger">{{ err }}</p>
    <p v-if="msg" class="tag ok">{{ msg }}</p>
    <div class="panel">
      <h3>水位线</h3>
      <table>
        <thead>
          <tr>
            <th>源</th><th>当前水位</th><th>最近成功提交</th><th>最近闸检</th><th>一致</th><th>lookback_seconds</th><th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="m in marks" :key="m.source_id">
            <td>{{ m.source_id }}. {{ m.name }}</td>
            <td class="mono">{{ m.watermark_ts || '—' }}</td>
            <td class="mono">
              <template v-if="m.last_commit">
                run #{{ m.last_commit.run_id }} · {{ m.last_commit.committed_watermark_ts || '—' }}
              </template>
              <template v-else>—</template>
            </td>
            <td>
              <template v-if="gateTag(m)">
                run #{{ m.last_gate.run_id }}
                <span class="tag" :class="gateTag(m).cls">{{ gateTag(m).text }}</span>
              </template>
              <span v-else class="muted">—</span>
            </td>
            <td>
              <span class="tag" :class="m.consistent ? 'ok' : 'danger'">
                {{ m.consistent ? '一致' : '不一致' }}
              </span>
            </td>
            <td><input type="number" v-model.number="m.lookback_seconds" min="0" style="width:7rem" /></td>
            <td><button class="secondary" @click="saveLookback(m)">保存 lookback</button></td>
          </tr>
        </tbody>
      </table>
    </div>
    <div class="panel">
      <h3>设置</h3>
      <table>
        <thead><tr><th>key</th><th>value</th><th></th></tr></thead>
        <tbody>
          <tr v-for="s in settings" :key="s.key">
            <td class="mono">{{ s.key }}</td>
            <td><input v-model="s.value" style="width:16rem" /></td>
            <td><button class="secondary" @click="saveSetting(s)">保存</button></td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
