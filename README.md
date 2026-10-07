# Virtual Pianist

ピアニストの「個性」を選んで、PCでリアルタイムにピアノを演奏させるアプリです。
音源は2種類あります。

- **Salamander Grand Piano（録音）**：`sounds/` に置いた SFZ 音源。ヤマハのグランドピアノ C5 を16段階の強さで録音したもの（既定）
- **合成ピアノ**：音源ファイル不要。録音ピアノを分析した倍音の表（約30KB）をもとに、numpy で正弦波を重ねて合成

ギター（クラシック／アコースティック）とエレキベースでも弾けます（どちらも合成音。ピアノの楽譜を自動で編曲します）。

## 起動

```
pip install -r requirements.txt
python main.py
```

合成ピアノは、初回だけ音の合成に10秒ほどかかります（`cache/` に保存され、2回目からはすぐ鳴ります）。
`sounds/` 以下で最初に見つかった `.sfz` が録音ピアノとして読み込まれます（16bit / 44.1kHz の WAV に対応）。

## 使い方

- **曲**：バッハ（平均律、トッカータとフーガ）、ベートーヴェン（エリーゼのために 全曲／冒頭の主題、「悲愴」「月光」ソナタ）、ロシア民謡「コロブチカ」（テトリスの曲、ピアノ用に編曲）、ショパン（10曲）、ドビュッシー（3曲）、サティ「ジムノペディ第1番」、ムソルグスキー「展覧会の絵」。`pieces/` に MIDI を置くと自動で一覧に追加されます。「MIDIを開く…」からも読み込めます
- **『のだめカンタービレ』の曲**：「悲愴」「月光」。曲名に〔のだめカンタービレ〕と表示されます
- **『蜜蜂と遠雷』の曲**：第1次予選の平均律（風間塵・高島明石・栄伝亜夜・マサルの4人分）、ショパン「黒鍵」、バラード第1番、「展覧会の絵」。曲名に〔蜜蜂と遠雷：弾いた人〕と表示されます
- **ピアニスト**：演奏の特徴を誇張したプリセット（機械的／グールド風／ホロヴィッツ風／ルービンシュタイン風／アルゲリッチ風／コルトー風）
- **表現スライダー**：テンポ、ルバート、強弱の幅、メロディの強調、和音のばらし、ペダルなど。再生中に動かすと約0.25秒後から反映されます
- **おまかせ連続演奏**：曲とピアニストをランダムに組み合わせて、次々に演奏します。全曲を一巡するまで同じ曲は出ません。「⏭ 次へ」で飛ばせ、「長さの上限」（既定12分）を超える曲は選ばれません。「■ 停止」で終了します
- **響き**：音源、会場（ドライ／サロン／コンサートホール／大聖堂）、残響の量、弦の共鳴、機械ノイズ

### ギター・ベース版

「音源」でギターかベースを選ぶと、同じ曲をその楽器向けに編曲して演奏します。奏者の一覧も楽器に合わせて切り替わります。

| 音源 | パート | 奏者 |
|---|---|---|
| クラシックギター（ナイロン弦）／アコースティックギター（スチール弦） | メロディ＋伴奏／メロディだけ | クラシックギター風（歌うアルペジオ）、弾き語り風（ストローク）、フラメンコ風、ジャズ・ギター風、ボサノヴァ風 |
| エレキベース | ベースライン／ベースライン（8分で刻む）／メロディ | フィンガー風、ロック風、ファンク風、バラード風 |
| ギター＋ベース（アコースティック／クラシック） | メロディ＋伴奏＋ベース／同（ベースは8分で刻む）／伴奏＋ベース（メロディなし） | ギターと同じ |

「ギター＋ベース」はピアノを使わず、ギターとベースの2人で合奏します。ベースがバスの声部を、ギターがそれ以外の声部を受け持ちます。

- **編曲**：音域外の音はオクターブ移動して収めます。ギターは同時に鳴る音を6本の弦までに減らし（メロディとバスを優先）、同じ高さの音は弾き直します。ベースはバスの声部を単音の線として取り出します
- **つまみ**：ギター・ベースでは「ペダル」が「響かせる（レットリング）」に、「和音のばらし」が「ストローク・アルペジオ」になります。「アップストローク」を上げると、裏拍の和音を高い弦からかき鳴らします
- 「弦の共鳴」（ピアノの共鳴弦）とペダルの音は、ギター・ベースでは鳴りません

WAVに書き出す場合：

```
python main.py --render out.wav --piece pieces/debussy_clair_de_lune.mid --preset コルトー
python main.py --render out.wav --piece elise --instrument synth   # 合成ピアノで書き出す
python main.py --render out.wav --piece bach_wtc1-02 --preset グールド   # 前奏曲とフーガを続けて
python main.py --render out.wav --piece elise --instrument nylon --preset クラシック   # クラシックギター
python main.py --render out.wav --piece korobeiniki --instrument steel --preset 弾き語り
python main.py --render out.wav --piece korobeiniki --instrument bass --part bass8 --preset ロック
python main.py --render out.wav --piece korobeiniki --instrument band_steel --preset 弾き語り   # ギター＋ベース
python main.py --list   # 奏者とパートの一覧
```

### 曲名の登録（`pieces/catalog.toml`）

曲名・組曲・テンポ補正は `pieces/catalog.toml` にまとめてあります。曲を追加するときは、MIDI を `pieces/` に置き、このファイルに1行足してアプリを起動し直してください。

```toml
[pieces]
"my_song" = { composer = "作曲者", title = "曲名" }    # pieces/my_song.mid

[sets."my_sonata"]                                      # 複数ファイルを続けて演奏
composer = "作曲者"
title = "ソナタ 全楽章"
files = ["my_sonata/mov1", "my_sonata/mov2"]
gap = 4.0

[tempo]
"my_song" = 120                                         # MIDIにテンポが無いときだけ
```

書き間違いがあると、起動時に警告が表示されます。

### 自分で追加する曲

IMSLP は自動ダウンロードを受け付けていないため、次の曲はブラウザで保存してください。
決まったファイル名で `pieces/` に置くと、日本語の曲名で一覧に並びます。

| 曲 | 入手先 | 保存するファイル名 |
|---|---|---|
| リスト：ハンガリー狂詩曲 第2番 | [IMSLP](https://imslp.org/wiki/Hungarian_Rhapsody_No.2,_S.244/2_(Liszt,_Franz))の「Synthesized/MIDI」欄（入力：IiAvoe、CC BY 4.0） | `liszt_hungarian_rhapsody2.mid` |
| リスト：愛の夢 第3番 | 自分で入手（下記） | `liszt_liebestraum3.mid` |
| サティ：ジュ・トゥ・ヴ | 自分で入手（下記） | `satie_je_te_veux.mid` |
| ラヴェル：亡き王女のためのパヴァーヌ | 自分で入手（下記） | `ravel_pavane.mid` |
| ショパン：練習曲「別れの曲」Op.10-3 | 自分で入手（下記） | `chopin_etude_op10-3.mid` |
| モーツァルト：きらきら星変奏曲 K.265 | 自分で入手（下記） | `mozart_twinkle_variations_k265.mid` |
| モーツァルト：2台のピアノのためのソナタ K.448 | 自分で入手（下記） | `mozart_sonata_2pianos_k448.mid` |
| シューベルト：ピアノ・ソナタ第16番 D845 | 自分で入手（下記） | `schubert_sonata_d845.mid` |
| バルトーク：アレグロ・バルバロ | 自分で入手（下記） | `bartok_allegro_barbaro.mid` |

「自分で入手」の曲は、自由なライセンスで自動取得できるMIDIが見つかりませんでした（2026年10月時点）。
[piano-midi.de](http://www.piano-midi.de/)（CC BY-SA）や MuseScore.com などで探し、ライセンスを確認して保存してください。

### 演奏の録音から MIDI を作る（自動採譜）

`transcribe.py` は、ピアノ演奏の録音（YouTube などのURL、または音声ファイル）を ByteDance の
[piano_transcription_inference](https://github.com/bytedance/piano_transcription) で採譜し、MIDI にします。
MIDI は `pieces/youtube/` に保存され、`catalog.toml` にも自動で登録されます。

GUI では「URLから採譜…」ボタンから、URL・曲名・作曲者・切り出し範囲を入力して変換できます
（クリップボードにURLがあれば自動で入ります）。変換中も演奏でき、終わると曲の一覧に追加されます。
コマンドからは次のようにします。

```
python transcribe.py https://youtu.be/XXXX --title "曲名" --composer "作曲者"
python transcribe.py https://youtu.be/XXXX --start 0:12 --end 3:40   # 演奏部分だけ切り出す
python transcribe.py recording.wav --name my_piece
```

- 必要なもの: `pip install torch --index-url https://download.pytorch.org/whl/cu128`（GPUなしなら通常の `pip install torch`）、
  `pip install piano_transcription_inference audioread`、`yt-dlp` と `ffmpeg`（URLから取り込む場合）
- 学習済みモデル（約170MB）は `~/piano_transcription_inference_data/note_F1=0.9677_pedal_F1=0.9186.pth` に置きます。
  無いと Linux の `wget` で取りに行こうとして失敗するので、
  [Zenodo](https://zenodo.org/record/4034264) から手動で保存してください。
- ピアノソロの録音ほど正確です。テンポ情報は入らないため、拍の位置は MIDI の小節線とずれます。
- YouTube の動画は、ダウンロードが認められているもの（自分の演奏など）に使い、作った MIDI は個人の練習用にとどめてください。

#### ギター・ベースの採譜

ギターやベースの演奏からも MIDI を作れます。ピアノ用のモデルは使わず、次の2段階で変換します。

1. **音源分離**：Meta の [Demucs](https://github.com/facebookresearch/demucs) で、バンド演奏からギター（`htdemucs_6s`）やベース（`htdemucs`）の音だけを取り出す
2. **採譜**：Spotify の [Basic Pitch](https://github.com/spotify/basic-pitch)（楽器を問わない採譜モデル）で MIDI にする

GUI では「URLから採譜…」の「採譜する楽器」でギターかベースを選びます。その楽器だけの録音なら、「音源分離」のチェックを外すと速く、正確になります。
MIDI は `pieces/youtube/<曲名>_guitar.mid`（ベースは `_bass.mid`）に保存され、曲名に（ギター）（ベース）が付きます。
「音源」でギターやベースを選んで演奏してください。

```
python transcribe.py https://youtu.be/XXXX --instrument guitar             # バンド演奏からギターを採譜
python transcribe.py https://youtu.be/XXXX --instrument bass --start 0:12  # ベース
python transcribe.py bass_solo.wav --instrument bass --no-split            # ベースだけの録音（音源分離しない）
```

- 必要なもの（Python 3.12 以降では、basic-pitch の依存関係に古い TensorFlow が指定されていて入らないので、`--no-deps` で入れます）:

  ```
  pip install basic-pitch --no-deps
  pip install onnxruntime pretty_midi "resampy<0.4.3" mir_eval scikit-learn librosa soundfile "setuptools<81"
  pip install demucs        # 音源分離を使う場合（torch も必要。ピアノの採譜と同じものでよい）
  ```

- Demucs のモデル（数十〜百MB）は、初回の変換で自動的にダウンロードされます。分離は GPU があれば速く、CPU では曲の長さと同じくらいかかります
- ギターは和音や速いフレーズほど誤りが増えます。ベースは単音として整理します（同時に鳴った音は強いほうだけを残す）

## 音づくり

### 録音ピアノ（Salamander）

録音にはピアノ本体の音色、響板の鳴り、マイクの定位がすべて含まれています。アプリ側では次のものを足しています。

- 録音されていない鍵盤は、短3度おきのサンプルを再生速度で音程補正する
- 鍵盤を離したときに、録音された「弦の余韻」と「ハンマー・ダンパーの音」を鳴らす（押していた時間が長いほど小さくなる）
- 録音されたペダルの踏み込み音・戻り音を鳴らす
- ホール残響と弦の共鳴（録音はピアノの弦の真上で近接収録されているため、会場の響きを足す）

### 合成ピアノ

実行時に WAV は使いません。ただし音色は、録音ピアノ（Salamander）を分析して作った表
`play_piano/piano_model.npz`（約30KB）に合わせています。表は `tools/build_piano_model.py` で作り直せます（`sounds/` の音源が必要です）。

| 要素 | 内容 |
|---|---|
| 弦 | 録音から測った倍音ごとの周波数（非調和性・ストレッチ調律）と、時間ごとの音量（二段階の減衰を含む）を正弦波で再現。1鍵につき1〜3本の弦をわずかにずらしてうなりを出す |
| 打鍵 | 弱打と強打の録音の両方に合わせ、その間はベロシティの2乗で混ぜる（高い倍音は強く弾いたときだけ急に増える） |
| 打撃音 | ハンマーが当たったときの響板・ケースの「コツン」。録音の倍音の間の成分を測り、そのスペクトルと減衰どおりに雑音を整形する |
| ステレオ | 奏者側に置いたマイクを想定。低音は左、高音は右に、時間差をつけて定位 |
| 弦の共鳴 | ペダルを踏むと88本すべての弦が共鳴する。ダンパーの無い高音弦は常に共鳴する |
| 会場 | 合成したホールのインパルス応答を畳み込む（低音は長く、高音は短く残響する） |
| 機械ノイズ | ダンパーが弦に触れる音、ペダルの踏み込みと戻りの音 |

### ギター・ベース

`play_piano/strings.py` で、音源ファイルを使わずに合成します（初回だけ数秒かかり、`cache/` に保存されます）。

| 要素 | 内容 |
|---|---|
| 弦 | はじく位置で決まる倍音の強さ（三角形の変位）と、高い倍音ほど早く消える減衰を正弦波で再現。弦の2方向の振動による余韻のうなり |
| 強弱 | 強くはじくほど高い倍音が増える（ピアノと同じく弱・強の2層を混ぜる） |
| 胴（ギター） | 胴の空気・表板の共鳴を周波数特性として掛け、はじいた瞬間の胴鳴りとピック（爪）の音を足す |
| ピックアップ（ベース） | ピックアップの位置で決まる倍音の強弱と、アンプの高域の上限 |

## 構成

| ファイル | 役割 |
|---|---|
| `play_piano/synth.py` | ピアノ音の合成（倍音と打撃音） |
| `play_piano/strings.py` | ギター・ベース音の合成（撥弦、胴、ピックアップ） |
| `play_piano/arrange.py` | ピアノの楽譜をギター・ベース向けに編曲 |
| `play_piano/piano_model.npz` | 合成ピアノの倍音・打撃音の表 |
| `tools/build_piano_model.py` | 録音ピアノを分析して上の表を作る |
| `play_piano/sampler.py` | SFZ 音源の読み込みと再生（メモリマップ、音程補正、リリース音） |
| `play_piano/acoustics.py` | ホール残響、弦の共鳴、分割畳み込み |
| `play_piano/engine.py` | リアルタイム発音（ダンパー、ハーフペダル、リミッター） |
| `play_piano/score.py` | 曲データとMIDIの読み込み（テンポや拍子の変化、両手、強弱、ペダル） |
| `play_piano/expression.py` | 個性パラメータ → タイミング、強弱、ペダルへの変換 |
| `play_piano/gui.py` | 画面 |

## 出典

### 音源

[Salamander Grand Piano V3](https://freepats.zenvoid.org/Piano/acoustic-grand-piano.html) — 作者：Alexander Holm（[CC BY 3.0](https://creativecommons.org/licenses/by/3.0/)）

### 曲データ

`pieces/` の MIDI は [Mutopia Project](https://www.mutopiaproject.org/) から取得しました。
次の曲以外はパブリックドメインです。

- ショパン：ノクターン Op.9-2 — 入力：Renato Biolcati Rinaldi（[CC BY-SA 3.0](https://creativecommons.org/licenses/by-sa/3.0/)）
- ショパン：バラード第1番 Op.23 — 入力：Javier Ruiz-Alma（[CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/)）
- ドビュッシー：アラベスク第2番 — 入力：Knute Snortum（[CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/)）
- バッハ：平均律 第1巻 第2番 フーガ — 入力：Urs Metzger（[CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/)）
- バッハ：平均律 第1巻 第5番 フーガ — 入力：Sven Reichard（[CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/)）
- ムソルグスキー：展覧会の絵 — 入力：Knute Snortum（[CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/)）
- ベートーヴェン：ピアノ・ソナタ第14番「月光」 — 入力：Stewart Holmes（[CC BY-SA 2.5](https://creativecommons.org/licenses/by-sa/2.5/)）
