// Isolated users/HTTPS; reads real project telemetry. No role changes in public.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { createHmac } from 'node:crypto';
import { pathToFileURL } from 'node:url';
const native=String.raw`\\wsl.localhost\Ubuntu\home\demus\.local\share\monotrajectoryweb`;
const [fixtureRoot,evidenceRoot]=process.argv.slice(2);
assert.match(fixtureRoot??'',/^\/home\/demus\/\.local\/share\/monotrajectoryweb\/web\/reports\/auth-browser-[a-f0-9]{32}$/);
assert.match(evidenceRoot??'',/^\/home\/demus\/\.local\/share\/monotrajectoryweb\/web\/reports\/admin-[a-f0-9]{32}$/);
const local=root=>path.join(native,root.split('/monotrajectoryweb/')[1]);
const dir=local(fixtureRoot),output=local(evidenceRoot);
const fixture=JSON.parse(await fs.readFile(path.join(dir,'credentials.json'),'utf8'));
const seed=await fs.readFile(path.join(dir,'fixture-seed'));
for(const user of Object.values(fixture.users))user.password=createHmac('sha256',seed).update(user.login).digest('hex');
const redact=v=>Object.values(fixture.users).reduce((s,u)=>s.replaceAll(u.password,'[redacted]'),String(v));
const base=fixture.base;assert.equal(base,'https://127.0.0.1:8444');
const {chromium}=await import(pathToFileURL(path.join(native,'frontend-deps/node_modules/playwright-core/index.mjs')).href);
const browser=await chromium.launch({channel:'chrome',headless:true});
const report={passed:false,base,browser:browser.version(),ignoreHTTPSErrors:true,checks:{},errors:[],mixed_content:[],blocked:[],monitor_requests:0,metrics:[]};
const a=await browser.newContext({ignoreHTTPSErrors:true,viewport:{width:1440,height:1000}});
const b=await browser.newContext({ignoreHTTPSErrors:true});
for(const c of [a,b])await c.route('**/*',route=>{
  const u=new URL(route.request().url());
  if(!['127.0.0.1','localhost'].includes(u.hostname)){report.blocked.push(u.origin);return route.abort();}
  return route.continue();
});
const pa=await a.newPage(),pb=await b.newPage();
for(const p of [pa,pb]){
  p.on('pageerror',e=>report.errors.push(redact(e.message)));
  p.on('console',m=>{if(/mixed content/i.test(m.text()))report.mixed_content.push(redact(m.text()));});
}
pa.on('request',r=>{if(r.url()===base+'/api/admin/monitoring')report.monitor_requests++;});
async function login(page,user){
  await page.goto(base);await page.locator('#login').fill(user.login);await page.locator('#password').fill(user.password);
  await page.getByRole('button',{name:'Увійти',exact:true}).click();
  await page.waitForFunction(login=>document.querySelector('.current-user')?.textContent===login,user.login);
}
async function sampleUntil(predicate,seconds=120){
  const deadline=Date.now()+seconds*1000;
  while(Date.now()<deadline){
    const response=await pa.waitForResponse(r=>r.url()===base+'/api/admin/monitoring'&&r.status()===200,{timeout:10000});
    const data=await response.json();
    report.metrics.push(data);if(report.metrics.length>70)report.metrics.shift();
    if(predicate(data))return data;
  }
  throw new Error('Monitor condition deadline exceeded');
}
try{
  assert.equal((await a.request.get(base+'/admin')).status(),401);
  assert.equal((await a.request.get(base+'/api/admin/monitoring')).status(),401);
  await login(pa,fixture.users.a);await login(pb,fixture.users.b);
  assert.equal((await b.request.get(base+'/admin')).status(),403);
  assert.equal((await b.request.get(base+'/api/admin/monitoring')).status(),403);
  assert.equal(await pb.getByRole('link',{name:'Моніторинг',exact:true}).count(),0);
  assert.equal((await a.request.get(`${base}/api/jobs/${fixture.users.b.job_id}`)).status(),404);
  report.checks.anonymous_401_regular_403_admin_200_and_ownership=true;
  await pa.getByRole('link',{name:'Моніторинг',exact:true}).click();
  await pa.getByRole('heading',{name:'Панель адміністратора',exact:true}).waitFor();
  await sampleUntil(x=>x.status==='available'&&x.capacity.active===0,20);
  await pa.screenshot({path:path.join(output,'admin-idle.png'),fullPage:true});
  await fs.writeFile(path.join(output,'browser-ready'),'ready');
  const active=await sampleUntil(x=>x.processes.some(p=>p.role==='native'&&p.cpu_percent>0&&p.rss_bytes>0));
  const p=active.processes.find(p=>p.role==='native'&&p.cpu_percent>0);
  await pa.waitForFunction(({pid,rss,cpu})=>Array.from(document.querySelectorAll('tr[data-role="native"]')).some(r=>r.textContent.includes(String(pid))&&r.textContent.includes(rss)&&r.textContent.includes(cpu)),
    {pid:p.host_pid,rss:(p.rss_bytes/1048576).toFixed(1)+' MiB',cpu:p.cpu_percent.toFixed(1)+'%'});
  report.active_native=p;await pa.screenshot({path:path.join(output,'admin-active.png'),fullPage:true});
  report.checks.real_worker_native_cpu_rss_visible=true;
  await sampleUntil(x=>x.capacity.active===0&&!x.processes.some(p=>p.job_id===report.active_native.job_id&&p.role==='native'),100);
  report.checks.slot_released_and_native_removed=true;
  await pa.screenshot({path:path.join(output,'admin-finished.png'),fullPage:true});
  await fs.writeFile(path.join(output,'browser-ready-for-container'),'ready');
  const container=await sampleUntil(x=>x.processes.some(p=>p.server_id==='compute-3'&&p.pid_scope==='container:compute-3'),100);
  report.container=container.processes.filter(p=>p.server_id==='compute-3');
  await pa.screenshot({path:path.join(output,'admin-container.png'),fullPage:true});
  report.checks.container_host_and_local_pids_visible=true;
  await fs.writeFile(path.join(output,'container-viewed'),'viewed');
  await sampleUntil(x=>!x.processes.some(p=>p.server_id==='compute-3'),20);
  await fs.writeFile(path.join(output,'browser-ready-for-stale'),'ready');
  await sampleUntil(x=>x.stale,40);
  await pa.getByRole('alert').waitFor();await pa.screenshot({path:path.join(output,'admin-stale.png'),fullPage:true});
  report.checks.stale_explicit=true;await fs.writeFile(path.join(output,'stale-viewed'),'viewed');
  await sampleUntil(x=>!x.stale,40);
  await pa.getByRole('link',{name:'До задач',exact:true}).click();await pa.locator('.history-total').waitFor();
  const count=report.monitor_requests;await pa.waitForTimeout(2300);assert.equal(report.monitor_requests,count);
  report.checks.polling_stops_after_navigation=true;
  await pa.getByRole('link',{name:'Моніторинг',exact:true}).click();await sampleUntil(x=>!x.stale,15);
  await pa.getByRole('button',{name:'Вийти',exact:true}).click();await pa.locator('#login').waitFor();
  const count2=report.monitor_requests;await pa.waitForTimeout(2300);assert.equal(report.monitor_requests,count2);
  assert.equal((await a.request.get(base+'/api/admin/monitoring')).status(),401);
  report.checks.polling_stops_after_logout=true;
  assert.deepEqual(report.errors,[]);assert.deepEqual(report.mixed_content,[]);assert.deepEqual(report.blocked,[]);
  report.passed=true;
}catch(e){report.errors.push(redact(e.stack??e));throw new Error(redact(e.message));}
finally{
  report.finished_at=new Date().toISOString();await fs.writeFile(path.join(output,'browser.json'),JSON.stringify(report,null,2));
  await a.close();await b.close();await browser.close();
  console.log(JSON.stringify({passed:report.passed,checks:report.checks,errors:report.errors}));
}
