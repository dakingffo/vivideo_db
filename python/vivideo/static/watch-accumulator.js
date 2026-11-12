/* Active visible playback time, independent of UI and provider SDKs. */
(function(root){
 class WatchAccumulator {
  constructor(){this.watched=0;this.duration=0;this.last=null;this.unstableDuration=false}
  reset(){this.last=null}
  sample({position,duration,playing,visible=true,seeking=false,rate=1},now){
   const valid=Number.isFinite(position)&&position>=0&&Number.isFinite(duration)&&duration>0&&duration<=604800;
   if(!valid){this.reset();return this.watched}
   if(this.duration&&Math.abs(this.duration-duration)>1){this.unstableDuration=true;this.reset()}
   this.duration=duration;
   const active=Boolean(playing&&visible&&!seeking&&!this.unstableDuration);
   const current={position,now,rate,active};const previous=this.last;this.last=current;
   if(!previous||!active||!previous.active||rate!==previous.rate||!Number.isFinite(rate)||rate<=0||rate>16)return this.watched;
   const elapsed=(now-previous.now)/1000;const progress=position-previous.position;
   // Large time gaps and jumps are not playback. Pauses/buffering never earn wall time.
   if(elapsed<=0||elapsed>2.5||progress<=0||progress>elapsed*rate+.35)return this.watched;
   this.watched+=Math.min(elapsed,progress/rate);
   return this.watched;
  }
  get qualified(){return !this.unstableDuration&&this.duration>0&&this.watched>this.duration/4}
 }
 root.VivideoWatchAccumulator=WatchAccumulator;
 if(typeof module!=='undefined'&&module.exports)module.exports=WatchAccumulator;
})(typeof window!=='undefined'?window:globalThis);
