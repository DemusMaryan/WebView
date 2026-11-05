// One final HTTPS UI run. Existing inputs only; no automatic retry or account creation.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {createHash} from 'node:crypto';
import {setTimeout as delay} from 'node:timers/promises';

const native=String.raw`\\wsl.localhost\Ubuntu\home\demus\.local\share\monotrajectoryweb`;
const root=process.argv[2];
assert.match(root??'',/^\/home\/demus\/\.local\/share\/monotrajectoryweb\/web\/reports\/final-[0-9a-f]{32}$/);
const directory=path.join(native,root.split('/monotrajectoryweb/')[1]);
await assert.rejects(fs.access(path.join(directory,'job.json')), {code:'ENOENT'});
const credentials=JSON.parse(await fs.readFile(path.join(native,'web/reports/queue-356124ce0fb14080873fd255b3fbec2f/credentials.json'),'utf8'));
const base='https://127.0.0.1:8443';
const report={started_at:new Date().toISOString(),passed:false,ignoreHTTPSErrors:true,observations:[],canvases:[],errors:[],checks:{}};
const clean=value=>String(value).replaceAll(credentials.password,'[redacted]');
const hash=value=>createHash('sha256').update(value).digest('hex');
const save=()=>fs.writeFile(path.join(directory,'browser.json'),JSON.stringify(report,null,2));
const {chromium}=await import(pathToFileURL(path.join(native,'frontend-deps/node_modules/playwright-core/index.mjs')).href);
const browser=await chromium.launch({channel:'chrome',headless:true});
let context,page,job;
try {
  const trust=await browser.newContext({ignoreHTTPSErrors:false});
  try {await (await trust.newPage()).goto(base);report.chrome_default_trust={trusted:true};}
  catch(error){report.chrome_default_trust={trusted:false,error:clean(error.message).split('\n')[0]};}
  await trust.close();
  context=await browser.newContext({ignoreHTTPSErrors:true,viewport:{width:1440,height:1100},locale:'uk-UA'});
  await context.route('**/*',route=>new URL(route.request().url()).origin===base?route.continue():route.abort());
  page=await context.newPage();
  page.on('pageerror',error=>report.errors.push(clean(error.message)));
  page.on('console',msg=>{if(/mixed content/i.test(msg.text()))report.errors.push(clean(msg.text()));});
  page.on('response',async response=>{
    if(!job||response.url()!==`${base}/api/jobs/${job.id}/snapshot`||response.status()!==200)return;
    try {
      const s=await response.json();
      report.observations.push({at:new Date().toISOString(),status:s.status,stage:s.stage,frame_id:s.frame_id,
        processed_frames:s.processed_frames,tracking_state:s.tracking_state,map_id:s.map_id,segment_id:s.segment_id,
        pose_valid:s.pose_valid,points:s.points.length,points_sha256:hash(JSON.stringify(s.points)),frame_url:s.frame_url});
    }catch{/* A response cancelled by navigation has no body. */}
  });
  await page.goto(base);
  await page.locator('#login').fill(credentials.login);await page.locator('#password').fill(credentials.password);
  await page.getByRole('button',{name:'Увійти',exact:true}).click();
  await page.locator('.current-user').waitFor();
  assert.equal(await page.locator('.current-user').innerText(),credentials.login);
  const cookie=(await context.cookies()).find(c=>c.name==='mtw_session');
  assert.ok(cookie?.secure&&cookie.httpOnly&&cookie.sameSite==='Strict');
  report.checks.https_login_secure_cookie=true;
  await page.locator('#video').setInputFiles(path.join(native,'datasets/freiburg1_xyz-rgb-30fps-short-10s.mp4'));
  await page.locator('#calibration').fill(await fs.readFile(path.join(native,'datasets/freiburg1_xyz-rgb-30fps-short-10s.parameters.json'),'utf8'));
  const posted=page.waitForResponse(r=>r.url()===base+'/api/jobs'&&r.request().method()==='POST');
  await page.getByRole('button',{name:'Створити задачу',exact:true}).click();
  const response=await posted;assert.equal(response.status(),202);job=await response.json();
  await fs.writeFile(path.join(directory,'job.json'),JSON.stringify({id:job.id,created_at:job.created_at,frames:300}));
  report.job_id=job.id;report.post_http=response.status();await save();
  const deadline=Date.now()+170000;
  let lastCanvas=-1000;
  while(true){
    const state=await page.evaluate(async id=>(await fetch(`/api/jobs/${id}`)).json(),job.id);
    const latest=report.observations.at(-1);
    if(state.status==='running'&&latest?.points>0&&latest.frame_id>=lastCanvas+30&&report.canvases.length<3){
      const image=page.locator('.frame-host img');
      await image.waitFor();
      const label=await page.locator('.frame-label').innerText();
      const alt=await image.getAttribute('alt');assert.ok(alt.endsWith(label.replace('Кадр ','')));
      assert.equal(await image.getAttribute('data-job-id'),job.id);
      const png=await page.locator('.map-host canvas').screenshot();
      const name=`live-${latest.frame_id}.png`;await fs.writeFile(path.join(directory,name),png);
      report.canvases.push({at:new Date().toISOString(),observed_snapshot_frame:latest.frame_id,label,alt,
        shown_points:await page.locator('.viewer[aria-label="Розріджена карта"] .viewer-caption').innerText(),sha256:hash(png),file:name});
      lastCanvas=latest.frame_id;
    }
    await save();
    if(['succeeded','failed','cancelled','timed_out'].includes(state.status)){
      assert.equal(state.status,'succeeded',state.error??'Job did not succeed');report.final_state=state;break;
    }
    assert.ok(Date.now()<deadline,'Final UI run exceeded observation deadline');await delay(450);
  }
  await page.locator('.result-stats').waitFor();
  const live=report.observations.filter(s=>s.status==='running'&&s.points>0);
  assert.ok(new Set(live.map(s=>s.frame_id)).size>=2,'Missing distinct live frame snapshots');
  assert.ok(new Set(live.map(s=>s.points_sha256)).size>=2,'Map did not change during processing');
  assert.ok(new Set(report.canvases.map(s=>s.sha256)).size>=2,'Missing changing live canvas screenshots');
  report.checks.progressive_snapshots_and_rendered_map=true;
  await page.reload();await page.locator('.result-stats').waitFor();
  assert.equal(await page.locator('.job-summary h1').innerText(),'Готово');
  assert.ok(await page.locator('.history').getByText(job.id.slice(0,8),{exact:true}).isVisible());
  report.result_text=await page.locator('.result-stats').innerText();
  await page.screenshot({path:path.join(directory,'result-before-restart.png'),fullPage:true});
  report.checks.result_and_history_after_reload=true;await save();
  await fs.writeFile(path.join(directory,'restart-ready'),new Date().toISOString());
  const restartDeadline=Date.now()+240000;
  while(true){try{await fs.access(path.join(directory,'restarted'));break;}catch{assert.ok(Date.now()<restartDeadline,'Restart handshake expired');await delay(500);}}
  await page.reload();await page.locator('.result-stats').waitFor();
  assert.equal(await page.locator('.current-user').innerText(),credentials.login);
  assert.equal(await page.locator('.job-summary h1').innerText(),'Готово');
  assert.ok(await page.locator('.history').getByText(job.id.slice(0,8),{exact:true}).isVisible());
  await page.screenshot({path:path.join(directory,'result-after-restart.png'),fullPage:true});
  report.checks.session_result_history_survive_restart=true;
  assert.deepEqual(report.errors,[]);
  await page.getByRole('button',{name:'Вийти',exact:true}).click();await page.locator('#login').waitFor();
  report.checks.logout=true;report.passed=true;
}catch(error){report.error=clean(error.stack??error);throw error;}
finally{report.finished_at=new Date().toISOString();await save();await browser.close();}
