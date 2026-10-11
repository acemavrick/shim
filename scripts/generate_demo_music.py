"""Render an original, gentle instrumental bed; no samples or external packages.

Usage: python scripts/generate_demo_music.py [output.wav] [seconds]
"""
from array import array
import math
from pathlib import Path
import random
import sys
import wave

RATE = 44100


def render(output: Path, seconds: float = 35):
    rng = random.Random(73)
    length = round(seconds * RATE)
    left, right = array('f', [0]) * length, array('f', [0]) * length
    beat = 60 / 96
    chords = [(50, 57, 61, 66), (47, 54, 57, 62), (43, 50, 54, 59),
              (45, 52, 57, 59), (47, 54, 57, 62), (43, 50, 54, 59), (50, 57, 61, 66)]

    def note(midi, at, duration, gain, pan=0, pad=False):
        frequency = 440 * 2 ** ((midi - 69) / 12)
        start, count = round(at * RATE), round(duration * RATE)
        lg, rg = math.sqrt((1 - pan) / 2), math.sqrt((1 + pan) / 2)
        phase_step = 2 * math.pi * frequency / RATE
        for i in range(max(0, -start), min(count, length - start)):
            t = i / RATE
            if pad:
                envelope = min(1, t / .45) * min(1, (duration - t) / .7)
                phase = phase_step * i + .012 * math.sin(2 * math.pi * .31 * t)
                tone = math.sin(phase) + .10 * math.sin(2 * phase)
            else:
                envelope = min(1, t / .012) * math.exp(-3.6 * t / duration) * min(1, (duration - t) / .09)
                phase = phase_step * i
                tone = math.sin(phase) + .20 * math.sin(2 * phase) + .045 * math.sin(3 * phase)
            value = gain * envelope * tone
            left[start + i] += value * lg
            right[start + i] += value * rg

    for bar in range(math.ceil(seconds / (8 * beat))):
        chord = chords[bar % len(chords)]
        at = bar * 8 * beat
        for index, pitch in enumerate(chord):
            note(pitch, at, 8 * beat + .55, .032, (index - 1.5) * .35, pad=True)
        for step in range(8):
            note(chord[0] - 12, at + step * beat + .005, .65, .054 if step % 4 == 0 else .028)
        # Space between soft electric-key notes keeps attention on the demo.
        pattern = [0, 2, 1, 3, 2, 1, 3, 2]
        for step, index in enumerate(pattern):
            note(chord[index] + 12, at + step * beat + rng.uniform(.02, .05),
                 1.05, .041 * rng.uniform(.78, 1.04), rng.uniform(-.35, .35))
        for step in range(8):
            start = round((at + step * beat + .5 * beat) * RATE)
            last_noise = 0.0
            for i in range(min(round(.055 * RATE), length - start)):
                noise = rng.uniform(-1, 1)
                high = noise - last_noise
                last_noise = noise
                value = .009 * high * math.exp(-i / (RATE * .011)) * min(1, i / 80)
                left[start + i] += value * .65
                right[start + i] += value * .8
        for step in (0, 4):
            start = round((at + step * beat) * RATE)
            for i in range(min(round(.20 * RATE), length - start)):
                t = i / RATE
                phase = 2 * math.pi * (48 * t + 22 * .04 * (1 - math.exp(-t / .04)))
                value = .072 * math.sin(phase) * min(1, t / .008) * math.exp(-t / .055)
                left[start + i] += value
                right[start + i] += value

    pcm = array('h')
    peak = max(max(abs(x) for x in left), max(abs(x) for x in right))
    for i, (a, b) in enumerate(zip(left, right)):
        t = i / RATE
        fade = min(1, t / .8) * min(1, max(0, seconds - t) / 1.7)
        scale = 0.43 / peak * fade
        pcm.extend((round(a * scale * 32767), round(b * scale * 32767)))
    if sys.byteorder != 'little':
        pcm.byteswap()
    output.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output), 'wb') as file:
        file.setnchannels(2)
        file.setsampwidth(2)
        file.setframerate(RATE)
        file.writeframes(pcm.tobytes())
    print(f'{output}: {seconds:.2f}s stereo, original instrumental, peak -7.33 dBFS')


if __name__ == '__main__':
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path('reports/demo/demo-music.wav')
    render(target, float(sys.argv[2]) if len(sys.argv) > 2 else 35)
