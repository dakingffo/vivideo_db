const assert=require('node:assert/strict');
const Watch=require('../python/vivideo/static/watch-accumulator.js');
function sample(w,position,now,extra={}){w.sample({position,duration:100,playing:true,visible:true,rate:1,...extra},now)}
const exact=new Watch();
for(let i=0;i<=25;i++)sample(exact,i,i*1000);
assert.equal(exact.watched,25);assert.equal(exact.qualified,false);
sample(exact,25.01,25010);assert.equal(exact.qualified,true);
const seek=new Watch();sample(seek,0,0);sample(seek,80,1000);assert.equal(seek.watched,0);assert.equal(seek.qualified,false);
sample(seek,81,2000);assert.equal(seek.watched,1);
const paused=new Watch();sample(paused,0,0);sample(paused,1,1000);sample(paused,1,2000,{playing:false});sample(paused,1,3000,{playing:false});sample(paused,1,4000);sample(paused,2,5000);assert.equal(paused.watched,2);
const hidden=new Watch();sample(hidden,0,0);sample(hidden,1,1000,{visible:false});sample(hidden,2,2000,{visible:false});sample(hidden,3,3000);sample(hidden,4,4000);assert.equal(hidden.watched,1);
const doubleSpeed=new Watch();for(let i=0;i<=20;i++)sample(doubleSpeed,i*2,i*1000,{rate:2});assert.equal(doubleSpeed.watched,20);assert.equal(doubleSpeed.qualified,false);
const stalled=new Watch();sample(stalled,0,0);sample(stalled,0,1000);sample(stalled,0,2000);assert.equal(stalled.watched,0);
const unknown=new Watch();for(let i=0;i<40;i++)sample(unknown,i,i*1000,{duration:NaN});assert.equal(unknown.qualified,false);
const live=new Watch();for(let i=0;i<40;i++)sample(live,i,i*1000,{duration:100+i*2});assert.equal(live.qualified,false);
const delayed=new Watch();sample(delayed,0,0);sample(delayed,20,20000);assert.equal(delayed.watched,0);
const replay=new Watch();sample(replay,10,0);sample(replay,11,1000);sample(replay,0,2000);sample(replay,1,3000);assert.equal(replay.watched,2);
console.log('Watch accumulator: strict threshold, seek, pause, hidden tab, speed, buffering, unknown/live duration, throttled clock, replay passed');
