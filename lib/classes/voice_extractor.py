import os
import regex as re
import subprocess
import shutil
import torch

from io import BytesIO
from pydub import AudioSegment
from torchvggish import vggish, vggish_input

from lib.conf import voice_formats
from lib.models import models

class VoiceExtractor:

    def __init__(self, session, models_dir, voice_file, voice_name):
        self.wav_file = None
        self.session = session
        self.voice_file = voice_file
        self.voice_name = voice_name
        self.models_dir = models_dir
        self.voice_track = 'vocals.wav'
        self.samplerate = models[session['tts_engine']][session['fine_tuned']]['samplerate']
        self.output_dir = self.session['voice_dir']
        self.demucs_dir = os.path.join(self.output_dir, 'htdemucs', os.path.splitext(os.path.basename(self.voice_file))[0])

    def _validate_format(self):
        file_extension = os.path.splitext(self.voice_file)[1].lower()
        if file_extension in voice_formats:
            msg = 'Input file valid'
            return True, msg
        error = f'Unsupported file format: {file_extension}. Supported formats are: {", ".join(voice_formats)}'
        return False, error

    def _convert_to_wav(self):
        try:
            self.wav_file = os.path.join(self.session['voice_dir'], os.path.basename(self.voice_file).replace(os.path.splitext(self.voice_file)[1], '.wav'))
            ffmpeg_cmd = [
                shutil.which('ffmpeg'), '-i', self.voice_file,
                '-ac', '1',
                '-ar', '44100',
                '-y', self.wav_file
            ]
            process = subprocess.Popen(
                ffmpeg_cmd,
                env={},
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                universal_newlines=True,
                encoding='utf-8'
            )
            for line in process.stdout:
                print(line, end='')  # Print each line of stdout
            process.wait()
            if process.returncode != 0:
                error = f'_convert_to_wav(): process.returncode: {process.returncode}'
            elif not os.path.exists(self.wav_file) or os.path.getsize(self.wav_file) == 0:
                error = f'_convert_to_wav output error: {self.wav_file} was not created or is empty.'                
            else:
                msg = 'Conversion to .wav format for processing successful'
                return True, msg
        except subprocess.CalledProcessError as e:
            error = f'convert_to_wav fmpeg.Error: {e.stderr.decode()}'
            raise ValueError(error)
        except Exception as e:
            error = f'_convert_to_wav() error: {e}'
            raise ValueError(error)
        return False, error
        
    def _detect_background(self):
        try:
            torch_home = os.path.join(self.models_dir, 'hub')
            torch.hub.set_dir(torch_home)
            os.environ['TORCH_HOME'] = torch_home
            energy_threshold = 8800 # to tune if not enough accurate (higher = less sensitive)
            model = vggish()
            model.eval()
            # Preprocess audio to log mel spectrogram
            log_mel_spectrogram = vggish_input.wavfile_to_examples(self.wav_file)
            audio_tensor = log_mel_spectrogram.clone().detach()
            embeddings = model(audio_tensor)
            # Calculate total energy
            energy_score = torch.norm(embeddings).item()           
            status = energy_score > energy_threshold
            msg = f'Noise Score: {energy_score:.2f}'
            if status:
                msg = f'{msg}\nBackground noise or music detected. Proceeding voice extraction.'
            else:
                msg = f'{msg}\nNo background noise or music detected. Skipping separation.'
            return True, status, msg
        except Exception as e:
            error = f'_detect_background() error: {e}'
            raise ValueError(error)
            return False, False, error

    def _demucs_voice(self):
        try:             
            cmd = [
                "demucs",
                "--verbose",
                "--two-stems=vocals",
                "--out", self.output_dir,
                self.wav_file
            ]
            try:
                torch_home = self.models_dir
                torch.hub.set_dir(torch_home)
                os.environ['TORCH_HOME'] = torch_home
                process = subprocess.run(cmd, check=True)
                self.voice_track = os.path.join(self.demucs_dir, self.voice_track)
                msg = 'Voice track isolation successful'
                return True, msg
            except subprocess.CalledProcessError as e:
                error = (
                    f'_demucs_voice() subprocess CalledProcessError error: {e.returncode}\n\n'
                    f'stdout: {e.output}\n\n'
                    f'stderr: {e.stderr}'
                )
                raise ValueError(error)
            except FileNotFoundError:
                error = f'_demucs_voice() subprocess FileNotFoundError error: The "demucs" command was not found. Ensure it is installed and in PATH.'
                raise ValueError(error)
            except Exception as e:              
                error = f'_demucs_voice() subprocess Exception error: {str(e)}'
                raise ValueError(error)
        except Exception as e:
            error = f'_demucs_voice() error: {e}'
            raise ValueError(error)
        return False, error
            
    def _remove_silences(self):
        try:
            audio = AudioSegment.from_file(self.voice_track)
            trimmed_audio = AudioSegment.silent(duration=0)
            for chunk in audio[::100]:
                if chunk.dBFS > -50:
                    trimmed_audio += chunk
            trimmed_audio.export(self.voice_track, format='wav')
            msg = 'Silences removed'
            return True, msg
        except Exception as e:
            error = f'_remove_silence() error: {e}'
            raise ValueError(e)
            return False, error

    def _normalize_audio(self):
        try:                 
            process_file = os.path.join(self.session['voice_dir'], f'{self.voice_name}.wav')
            ffmpeg_cmd = [shutil.which('ffmpeg'), '-i', self.voice_track]
            filter_complex = (
                'agate=threshold=-25dB:ratio=1.4:attack=10:release=250,'
                'afftdn=nf=-70,'
                'acompressor=threshold=-20dB:ratio=2:attack=80:release=200:makeup=1dB,'
                'loudnorm=I=-14:TP=-3:LRA=7:linear=true,'
                'equalizer=f=150:t=q:w=2:g=1,'
                'equalizer=f=250:t=q:w=2:g=-3,'
                'equalizer=f=3000:t=q:w=2:g=2,'
                'equalizer=f=5500:t=q:w=2:g=-4,'
                'equalizer=f=9000:t=q:w=2:g=-2,'
                'highpass=f=63[audio]'
            )
            ffmpeg_cmd += [
                '-filter_complex', filter_complex,
                '-map', '[audio]',
                '-ar', 'null',
                '-y', process_file
            ]
            error = None
            for rate in ['16000', '24000']:
                ffmpeg_cmd[-3] = rate
                ffmpeg_cmd[-1] = re.sub(r'\.wav$', f'_{rate}.wav', process_file)
                try:
                    process = subprocess.Popen(
                        ffmpeg_cmd,
                        env={},
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        universal_newlines=True,
                        encoding='utf-8'
                    )
                    for line in process.stdout:
                        print(line, end='')  # Print each line of stdout
                    process.wait()
                    if process.returncode != 0:
                        error = f'_normalize_audio(): process.returncode: {process.returncode}'
                        break
                    elif not os.path.exists(ffmpeg_cmd[-1]) or os.path.getsize(ffmpeg_cmd[-1]) == 0:
                        error = f'_normalize_audio() error: {ffmpeg_cmd[-1]} was not created or is empty.'
                        break
                except subprocess.CalledProcessError as e:
                    error = f'_normalize_audio() ffmpeg.Error: {e.stderr.decode()}'
                    break
            if error is None:
                shutil.rmtree(self.demucs_dir, ignore_errors=True)
                os.remove(process_file)
                msg = 'Audio normalization successful!'
                return True, msg
        except FileNotFoundError:
            error = '_normalize_audio() FileNotFoundError: Error: Input file or FFmpeg binary is missing!'
            raise ValueError(error)
        except Exception as e:
            error = f'_normalize_audio() error: {e}'
            raise ValueError(error)
        return False, error

    def extract_voice(self):
        success = False
        msg = None
        try:
            success, msg = self._validate_format()
            print(msg)
            if success:
                success, msg = self._convert_to_wav()
                print(msg)
                if success:
                    success, status, msg = self._detect_background()
                    print(msg)
                    if success:
                        if status:
                            success, msg = self._demucs_voice()
                            print(msg)
                        else:
                            self.voice_track = self.wav_file
                        if success:
                            success, msg = self._remove_silences()
                            print(msg)
                            if success:
                                success, msg = self._normalize_audio()
                                print(msg)
        except Exception as e:
            msg = f'extract_voice() error: {e}'
            raise ValueError(msg)
        shutil.rmtree(self.demucs_dir, ignore_errors=True)
        torch.hub.set_dir(self.models_dir)
        os.environ['TORCH_HOME'] = self.models_dir
        return success, msg