'use strict';
const assert=require('node:assert/strict');
const GalleryX=require('../extensions/gallery-qwen-x/shared.js');

function image({currentSrc='',src='',attrs={}}={}){
  return {currentSrc,src,getAttribute(name){return Object.prototype.hasOwnProperty.call(attrs,name)?attrs[name]:null;}};
}

const expected='https://pbs.twimg.com/media/photo_123?format=png&name=orig';
assert.equal(
  GalleryX.mediaKey(expected),
  'photo_123',
  'uses the stable X media id instead of the transient query string',
);
assert.equal(
  GalleryX.mediaKey('https://pbs.twimg.com/media/photo_123.png?format=png&name=large'),
  'photo_123',
  'ignores extension and size changes when deriving the media id',
);
assert.equal(
  GalleryX.mediaUrlFromImage(image({currentSrc:'blob:https://x.com/ignored',attrs:{'data-src':expected}})),
  expected,
  'falls back to a lazy data-src when currentSrc is a blob URL',
);
assert.equal(
  GalleryX.mediaUrlFromImage(image({attrs:{srcset:'https://pbs.twimg.com/media/photo_123?format=png&name=small 640w, https://pbs.twimg.com/media/photo_123?format=png&name=large 2048w'}})),
  expected,
  'accepts the largest valid X media candidate from srcset',
);
assert.equal(
  GalleryX.mediaUrlFromImage(image({attrs:{srcset:'//pbs.twimg.com/media/photo_123?format=png&name=small 1x'}})),
  expected,
  'normalizes protocol-relative CDN URLs',
);
assert.equal(
  GalleryX.mediaUrlFromImage(image({src:'https://pbs.twimg.com/profile_images/avatar.png'})),
  '',
  'does not treat profile images as post media',
);
assert.equal(
  GalleryX.mediaUrlFromImage(image({attrs:{'data-src':'https://pbs.twimg.com/ext_tw_video_thumb/123/pu/img/a.jpg'}})),
  '',
  'does not treat video thumbnails as post media',
);
console.log('PASS extension media candidate tests');
