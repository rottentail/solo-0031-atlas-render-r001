#!/usr/bin/env python3
"""Run bounded production flows against a temporary Atlas Render server."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zlib

ROOT = Path(__file__).resolve().parents[1]


def sample():
    return {"width": 320, "height": 240, "background": "#F5F1E8", "layers": [
        {"id": "plate", "kind": "rectangle", "width": 220, "height": 150,
         "transform": {"x": 45, "y": 35, "scaleX": 1, "scaleY": 1, "degrees": 5},
         "opacity": 1, "fill": {"kind": "linear", "color": "#145A70", "endColor": "#32A5A0", "from": {"x": 0, "y": 0}, "to": {"x": 220, "y": 150}}},
        {"id": "accent", "kind": "ellipse", "width": 130, "height": 130,
         "transform": {"x": 150, "y": 95, "scaleX": 1, "scaleY": 1, "degrees": 0},
         "opacity": .85, "fill": {"kind": "solid", "color": "#E87E4F"}},
        {"id": "inset", "kind": "group", "transform": {"x": 150, "y": 100, "scaleX": 1, "scaleY": 1, "degrees": -8}, "opacity": .9,
         "clip": {"x": 0, "y": 0, "width": 90, "height": 75}, "children": [
             {"id": "facet", "kind": "polygon", "width": 100, "height": 100,
              "transform": {"scaleX": 1, "scaleY": 1}, "opacity": .8,
              "fill": {"kind": "solid", "color": "#FFF4D5"}, "points": [{"x": 10, "y": 90}, {"x": 50, "y": 0}, {"x": 100, "y": 90}]}]}]}


def tiny_png():
    def chunk(tag, data):
        return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data))
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 2, 2, 8, 6, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(b'\0' + bytes([240, 40, 80, 255] * 2) + b'\0' + bytes([40, 80, 240, 128] * 2))) + chunk(b'IEND', b'')


class Demo:
    def __init__(self, binary, directory):
        self.binary = binary
        self.directory = directory
        self.proc = None
        self.log = open(directory / 'server.log', 'ab')
        self.start()

    def start(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        self.url = f'http://127.0.0.1:{port}'
        env = dict(os.environ, ATLAS_ADDR=f'127.0.0.1:{port}', ATLAS_STATE=str(self.directory / 'state.json'))
        self.proc = subprocess.Popen([str(self.binary)], env=env, stdout=self.log, stderr=self.log)
        for _ in range(100):
            try:
                self.call('GET', '/healthz')
                return
            except (urllib.error.URLError, ConnectionError):
                if self.proc.poll() is not None:
                    raise RuntimeError((self.directory / 'server.log').read_text())
                time.sleep(.03)
        raise RuntimeError('server startup deadline exceeded')

    def stop(self, abrupt=False):
        if self.proc and self.proc.poll() is None:
            (self.proc.kill if abrupt else self.proc.terminate)()
            self.proc.wait(timeout=15)

    def call(self, method, path, body=None, status=200, headers=None):
        hdr = dict(headers or {})
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
            hdr['Content-Type'] = 'application/json'
        request = urllib.request.Request(self.url + path, data=body, method=method, headers=hdr)
        try:
            response = urllib.request.urlopen(request, timeout=15)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            data = response.read()
            if response.status != status:
                raise RuntimeError(f'{method} {path}: expected {status}, received {response.status}: {data[:500]!r}')
            if response.headers.get('Content-Type', '').startswith('application/json'):
                return json.loads(data)
            return data

    def wait(self, job_id, wanted=('succeeded',)):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            job = self.call('GET', '/v1/jobs/' + job_id)
            if job['state'] in wanted:
                return job
            if job['state'] in ('failed', 'cancelled'):
                raise RuntimeError(f'unexpected job outcome: {job}')
            time.sleep(.025)
        raise RuntimeError('render deadline exceeded')

    def submit(self, snapshot, key='render-one', scale=1):
        return self.call('POST', '/v1/jobs', {'snapshotId': snapshot['id'], 'scale': scale}, 202, {'Idempotency-Key': key})


def stroked_sample():
    """Nested scene exercising rotation, non-uniform scale, clip and stroke."""
    return {"width": 320, "height": 240, "background": "#F5F1E8", "layers": [
        {"id": "plate", "kind": "rectangle", "width": 220, "height": 150,
         "transform": {"x": 45, "y": 35, "scaleX": 1, "scaleY": 1, "degrees": 5},
         "opacity": 1, "fill": {"kind": "linear", "color": "#145A70", "endColor": "#32A5A0",
                                "from": {"x": 0, "y": 0}, "to": {"x": 220, "y": 150}},
         "stroke": {"width": 4, "color": "#1C1C1C"}},
        {"id": "accent", "kind": "ellipse", "width": 130, "height": 130,
         "transform": {"x": 150, "y": 95, "scaleX": 1, "scaleY": 1, "degrees": 0},
         "opacity": .85, "fill": {"kind": "solid", "color": "#E87E4F"},
         "stroke": {"width": 3, "color": "#7A2E0E"}},
        {"id": "inset", "kind": "group",
         "transform": {"x": 150, "y": 100, "scaleX": 1.4, "scaleY": .7, "degrees": -8},
         "opacity": .9, "clip": {"x": 0, "y": 0, "width": 90, "height": 75}, "children": [
             {"id": "facet", "kind": "polygon", "width": 100, "height": 100,
              "transform": {"scaleX": 1, "scaleY": 1}, "opacity": .8,
              "fill": {"kind": "solid", "color": "#FFF4D5"},
              "stroke": {"width": 5, "color": "#B03A2E"},
              "points": [{"x": 10, "y": 90}, {"x": 50, "y": 0}, {"x": 100, "y": 90}]}]}]}


def run(demo, workflow, out):
    if workflow == 'stroke-render':
        return stroke_render(demo, out)
    d = sample()
    put = lambda doc, rev=0: demo.call('PUT', '/v1/scenes/emblem', {'name': '双色印记', 'expectedRevision': rev, 'document': doc})
    scene = put(d)
    if workflow == 'create-scene':
        assert demo.call('GET', '/v1/scenes/emblem')['document'] == scene['document']
        bad = sample(); bad['layers'][0]['transform']['scaleX'] = 0
        demo.call('PUT', '/v1/scenes/invalid', {'name': '无效几何', 'expectedRevision': 0, 'document': bad}, 400)
    elif workflow == 'revise-scene':
        d['background'] = '#F0F0F0'; assert put(d, 1)['revision'] == 2
        demo.call('PUT', '/v1/scenes/emblem', {'name': '旧版本', 'expectedRevision': 1, 'document': d}, 409)
    elif workflow == 'archive-scene':
        archived = demo.call('POST', '/v1/scenes/emblem/state', {'expectedRevision': 1, 'state': 'archived'})
        demo.call('POST', '/v1/scenes/emblem/snapshots', {'expectedRevision': archived['revision']}, 409)
        assert demo.call('POST', '/v1/scenes/emblem/state', {'expectedRevision': archived['revision'], 'state': 'active'})['revision'] == 3
    if workflow in ('import-asset', 'retire-asset'):
        asset = demo.call('PUT', '/v1/assets/ribbon', tiny_png(), 201)
        assert asset['width'] == 2 and asset['digest'] == hashlib.sha256(tiny_png()).hexdigest()
        demo.call('PUT', '/v1/assets/broken', b'not png', 400)
        layer = {'id': 'ribbon', 'kind': 'image', 'width': 60, 'height': 60, 'assetId': 'ribbon', 'opacity': 1, 'transform': {'x': 20, 'y': 30, 'scaleX': 1, 'scaleY': 1}}
        d['layers'].append(layer); put(d, 1)
        pinned = demo.call('POST', '/v1/scenes/emblem/snapshots', {'expectedRevision': 2}, 201)
        demo.call('POST', '/v1/assets/ribbon/retire')
        demo.call('POST', '/v1/scenes/emblem/snapshots', {'expectedRevision': 2}, 409)
        result = demo.wait(demo.submit(pinned)['id']); assert result['state'] == 'succeeded'
        return {'workflow': workflow, 'assetDigest': asset['digest'], 'pinnedRender': result['state']}
    current = demo.call('GET', '/v1/scenes/emblem')
    snapshot = demo.call('POST', '/v1/scenes/emblem/snapshots', {'expectedRevision': current['revision']}, 201)
    if workflow == 'freeze-scene':
        original_digest = snapshot['digest']; d['background'] = '#000000'; put(d, current['revision'])
        assert demo.call('GET', '/v1/snapshots/' + snapshot['id'])['digest'] == original_digest
    if workflow in ('export-package', 'import-package'):
        package = demo.call('GET', '/v1/snapshots/' + snapshot['id'] + '/package')
        restored = demo.call('POST', '/v1/imports/restored', {'name': '恢复的印记', 'package': package}, 201)
        assert restored['document'] == snapshot['document']
        corrupt = json.loads(json.dumps(package)); corrupt['snapshot']['document']['width'] += 1
        demo.call('POST', '/v1/imports/corrupt', {'name': '损坏包', 'package': corrupt}, 400)
        demo.call('GET', '/v1/scenes/corrupt', status=404)
    if workflow in ('cancel-render', 'retry-render', 'recover-render'):
        heavy = sample(); heavy['width'] = 1500; heavy['height'] = 1500
        heavy['layers'] = [dict(heavy['layers'][0], id=f'sheet-{i}', width=1500, height=1500, transform={'scaleX': 1, 'scaleY': 1}) for i in range(16)]
        put(heavy, current['revision'])
        snapshot = demo.call('POST', '/v1/scenes/emblem/snapshots', {'expectedRevision': current['revision'] + 1}, 201)
    job = demo.submit(snapshot)
    if workflow == 'submit-render':
        assert demo.submit(snapshot)['id'] == job['id']
        demo.call('POST', '/v1/jobs', {'snapshotId': snapshot['id'], 'scale': 2}, 409, {'Idempotency-Key': 'render-one'})
    if workflow == 'revoke-snapshot':
        demo.call('POST', '/v1/snapshots/' + snapshot['id'] + '/revoke')
        demo.call('POST', '/v1/jobs', {'snapshotId': snapshot['id'], 'scale': 1}, 409, {'Idempotency-Key': 'blocked-after-revoke'})
    if workflow in ('cancel-render', 'retry-render'):
        stopped = demo.call('POST', '/v1/jobs/' + job['id'] + '/cancel')
        assert stopped['state'] == 'cancelled'
        if workflow == 'cancel-render':
            time.sleep(.1); assert demo.call('GET', '/v1/jobs/' + job['id'])['state'] == 'cancelled'
            return {'workflow': workflow, 'state': 'cancelled'}
        job = demo.call('POST', '/v1/jobs/' + job['id'] + '/retry', status=202)
        assert job['attempt'] == 2
    if workflow == 'recover-render':
        demo.wait(job['id'], wanted=('running',)); demo.stop(abrupt=True); demo.start()
    result = demo.wait(job['id'])
    png = demo.call('GET', '/v1/artifacts/' + result['artifactId'])
    assert png[:8] == b'\x89PNG\r\n\x1a\n'
    assert struct.unpack('>II', png[16:24]) == (snapshot['document']['width'], snapshot['document']['height'])
    if out:
        Path(out).mkdir(parents=True, exist_ok=True); (Path(out) / 'atlas-sample.png').write_bytes(png)
    if workflow == 'compare-renders':
        other = demo.wait(demo.submit(snapshot, 'render-two')['id'])
        comparison = demo.call('POST', '/v1/compare', {'left': result['artifactId'], 'right': other['artifactId']})
        assert comparison['changedPixels'] == 0
        d['background'] = '#000000'; put(d, current['revision'])
        altered = demo.call('POST', '/v1/scenes/emblem/snapshots', {'expectedRevision': current['revision'] + 1}, 201)
        changed = demo.wait(demo.submit(altered, 'render-three')['id'])
        assert demo.call('POST', '/v1/compare', {'left': result['artifactId'], 'right': changed['artifactId']})['changedPixels'] > 0
    if workflow == 'purge-artifact':
        demo.call('DELETE', '/v1/artifacts/' + result['artifactId'])
        demo.call('GET', '/v1/artifacts/' + result['artifactId'], status=410)
        assert demo.call('GET', '/v1/jobs/' + job['id'])['state'] == 'succeeded'
    if workflow == 'observe-service':
        summary = demo.call('GET', '/v1/status'); assert summary['jobs']['succeeded'] >= 1
        events = demo.call('GET', '/v1/events'); assert any(e['kind'] == 'job.succeeded' for e in events)
    return {'workflow': workflow, 'state': result['state'], 'pngBytes': len(png), 'pngDigest': hashlib.sha256(png).hexdigest()}


def stroke_render(demo, out):
    """Exercise vector strokes end to end through the real HTTP server."""
    doc = stroked_sample()
    created = demo.call('PUT', '/v1/scenes/stroked', {'name': '描边印记', 'expectedRevision': 0, 'document': doc})
    assert created['revision'] == 1
    # Unknown fields and malformed strokes are rejected before entering any queue.
    bad_field = json.loads(json.dumps(doc))
    bad_field['layers'][0]['stroke']['dash'] = 4
    demo.call('PUT', '/v1/scenes/bad-stroke', {'name': '坏描边', 'expectedRevision': 0, 'document': bad_field}, 400)
    invalid_width = json.loads(json.dumps(doc))
    invalid_width['layers'][0]['stroke'] = {'width': 0, 'color': '#FF0000'}
    demo.call('PUT', '/v1/scenes/bad-stroke', {'name': '坏描边', 'expectedRevision': 0, 'document': invalid_width}, 400)
    invalid_color = json.loads(json.dumps(doc))
    invalid_color['layers'][0]['stroke'] = {'width': 4, 'color': 'red'}
    demo.call('PUT', '/v1/scenes/bad-stroke', {'name': '坏描边', 'expectedRevision': 0, 'document': invalid_color}, 400)
    invalid_target = json.loads(json.dumps(doc))
    invalid_target['layers'][2]['children'][0] = {
        'id': 'facet', 'kind': 'image', 'width': 100, 'height': 100, 'assetId': 'missing',
        'transform': {'scaleX': 1, 'scaleY': 1}, 'opacity': .8,
        'stroke': {'width': 5, 'color': '#B03A2E'}}
    demo.call('PUT', '/v1/scenes/bad-stroke', {'name': '坏描边', 'expectedRevision': 0, 'document': invalid_target}, 400)
    # Expensive strokes must be refused at submission (429), never queued.
    heavy = {'width': 1500, 'height': 1500, 'background': '#FFFFFF', 'layers': [
        dict(stroked_sample()['layers'][0], id=f'sheet-{i}', width=900, height=900,
             transform={'x': 55, 'y': 55, 'scaleX': 1, 'scaleY': 1, 'degrees': 0},
             stroke={'width': 110, 'color': '#FF0000'}) for i in range(40)]}
    demo.call('PUT', '/v1/scenes/heavy-stroke', {'name': '昂贵轮廓', 'expectedRevision': 0, 'document': heavy})
    heavy_snap = demo.call('POST', '/v1/scenes/heavy-stroke/snapshots', {'expectedRevision': 1}, 201)
    demo.call('POST', '/v1/jobs', {'snapshotId': heavy_snap['id'], 'scale': 1}, 429,
              {'Idempotency-Key': 'stroke-too-expensive'})
    queued = demo.call('GET', '/v1/status')['jobs'].get('queued', 0)
    assert queued == 0, 'over-budget stroke leaked into the queue'
    # The same geometry with an affordable width is accepted and rendered.
    for layer in heavy['layers']:
        layer['stroke']['width'] = 100
    demo.call('PUT', '/v1/scenes/heavy-stroke', {'name': '昂贵轮廓', 'expectedRevision': 1, 'document': heavy})
    cheap_snap = demo.call('POST', '/v1/scenes/heavy-stroke/snapshots', {'expectedRevision': 2}, 201)
    cheap_job = demo.wait(demo.submit(cheap_snap, 'stroke-affordable')['id'])
    assert cheap_job['state'] == 'succeeded'
    # Freeze, then render scale 1 and scale 2 from identical frozen inputs.
    snapshot = demo.call('POST', '/v1/scenes/stroked/snapshots', {'expectedRevision': 1}, 201)
    job1 = demo.wait(demo.submit(snapshot, 'stroke-scale-1', 1)['id'])
    job2 = demo.wait(demo.submit(snapshot, 'stroke-scale-2', 2)['id'])
    png1 = demo.call('GET', '/v1/artifacts/' + job1['artifactId'])
    png2 = demo.call('GET', '/v1/artifacts/' + job2['artifactId'])
    assert struct.unpack('>II', png1[16:24]) == (320, 240)
    assert struct.unpack('>II', png2[16:24]) == (640, 480)
    digest1, digest2 = hashlib.sha256(png1).hexdigest(), hashlib.sha256(png2).hexdigest()
    # Replay the same idempotency key: fixed inputs reproduce identical bytes.
    replay = demo.wait(demo.submit(snapshot, 'stroke-scale-1', 1)['id'])
    assert replay['id'] == job1['id']
    assert hashlib.sha256(demo.call('GET', '/v1/artifacts/' + replay['artifactId'])).hexdigest() == digest1
    # Exchange package round trip keeps stroke geometry and yields the same PNG.
    package = demo.call('GET', '/v1/snapshots/' + snapshot['id'] + '/package')
    restored = demo.call('POST', '/v1/imports/stroked-copy', {'name': '描边副本', 'package': package}, 201)
    assert restored['document'] == snapshot['document']
    assert restored['document']['layers'][0]['stroke'] == {'width': 4, 'color': '#1C1C1C'}
    copy_snap = demo.call('POST', '/v1/scenes/stroked-copy/snapshots', {'expectedRevision': 1}, 201)
    copy_job = demo.wait(demo.submit(copy_snap, 'stroke-package-copy', 1)['id'])
    digest_copy = hashlib.sha256(demo.call('GET', '/v1/artifacts/' + copy_job['artifactId'])).hexdigest()
    assert digest_copy == digest1, 'package round trip changed stroke rendering'
    if out:
        Path(out).mkdir(parents=True, exist_ok=True)
        (Path(out) / 'stroke-scale-1.png').write_bytes(png1)
        (Path(out) / 'stroke-scale-2.png').write_bytes(png2)
    return {'workflow': 'stroke-render', 'pngDigest1x': digest1, 'pngDigest2x': digest2,
            'packageDigestMatch': digest_copy == digest1, 'rejectedBeforeQueue': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workflow', default='submit-render')
    parser.add_argument('--out')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='atlas-walkthrough-') as temp:
        directory = Path(temp); binary = directory / 'atlas'
        subprocess.run(['go', 'build', '-o', str(binary), './cmd/atlas'], cwd=ROOT, check=True, timeout=60)
        demo = Demo(binary, directory)
        try:
            print(json.dumps(run(demo, args.workflow, args.out), ensure_ascii=False))
        finally:
            demo.stop(); demo.log.close()


if __name__ == '__main__':
    main()
