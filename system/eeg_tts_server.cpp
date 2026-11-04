// Chinese-only resident synthesis provider. It never opens audio, ADC or DRM.
#include "sherpa_c_api_v1_13_8.h"
#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

static bool read_all(int fd, void *ptr, size_t n) {
  auto *p = static_cast<char *>(ptr);
  while (n) { ssize_t k = recv(fd, p, n, 0); if (k <= 0) return false; p += k; n -= k; }
  return true;
}
static bool write_all(int fd, const void *ptr, size_t n) {
  auto *p = static_cast<const char *>(ptr);
  while (n) { ssize_t k = send(fd, p, n, MSG_NOSIGNAL); if (k <= 0) return false; p += k; n -= k; }
  return true;
}
static double seconds(std::chrono::steady_clock::time_point start) {
  return std::chrono::duration<double>(std::chrono::steady_clock::now()-start).count();
}
int main(int argc, char **argv) {
  try {
    if (argc != 4) throw std::runtime_error("usage: eeg-tts-server MODEL_DIR MODEL_FILE SOCKET");
    const std::string base=argv[1], model=base+"/"+argv[2], lex=base+"/lexicon.txt", tokens=base+"/tokens.txt";
    const std::string rules=base+"/phone.fst,"+base+"/date.fst,"+base+"/number.fst";
    const std::string socket_path=argv[3];
    if (socket_path.size() >= sizeof(sockaddr_un::sun_path)) throw std::runtime_error("socket path too long");
    SherpaOnnxOfflineTtsConfig c{};
    c.model.vits.model=model.c_str(); c.model.vits.lexicon=lex.c_str(); c.model.vits.tokens=tokens.c_str();
    c.model.vits.noise_scale=0.667f; c.model.vits.noise_scale_w=0.8f; c.model.vits.length_scale=1.0f;
    c.model.num_threads=2; c.model.provider="cpu"; c.rule_fsts=rules.c_str();
    c.max_num_sentences=1; c.silence_scale=0.2f;
    const auto start=std::chrono::steady_clock::now();
    std::unique_ptr<const SherpaOnnxOfflineTts, decltype(&SherpaOnnxDestroyOfflineTts)> tts(SherpaOnnxCreateOfflineTts(&c), SherpaOnnxDestroyOfflineTts);
    if (!tts) throw std::runtime_error("model initialization failed");
    int server=socket(AF_UNIX, SOCK_STREAM|SOCK_CLOEXEC, 0);
    if (server<0) throw std::runtime_error("socket failed");
    sockaddr_un addr{}; addr.sun_family=AF_UNIX; std::memcpy(addr.sun_path,socket_path.c_str(),socket_path.size()+1);
    // Never unlink a potentially live endpoint. systemd owns RuntimeDirectory.
    if (bind(server,reinterpret_cast<sockaddr *>(&addr),sizeof(addr)) || chmod(socket_path.c_str(),0600) || listen(server,1))
      throw std::runtime_error("socket bind/listen failed");
    std::fprintf(stderr,"EEG_TTS_READY initialization_seconds=%.6f threads=2 version=%s\n", seconds(start), SherpaOnnxGetVersionStr());
    std::fflush(stderr);
    for (;;) {
      int fd=accept4(server,nullptr,nullptr,SOCK_CLOEXEC); if(fd<0) continue;
      timeval timeout{10,0}; setsockopt(fd,SOL_SOCKET,SO_RCVTIMEO,&timeout,sizeof(timeout)); setsockopt(fd,SOL_SOCKET,SO_SNDTIMEO,&timeout,sizeof(timeout));
      ucred peer{}; socklen_t peer_size=sizeof(peer);
      uint32_t n=0; float speed=0;
      bool valid=getsockopt(fd,SOL_SOCKET,SO_PEERCRED,&peer,&peer_size)==0 && peer.uid==0 && read_all(fd,&n,sizeof(n)) && read_all(fd,&speed,sizeof(speed));
      valid=valid && n>0 && n<=16384 && std::isfinite(speed) && speed>=0.5f && speed<=2.8125f;
      std::string text(valid?n:0,'\0');
      valid=valid && read_all(fd,text.data(),text.size()) && text.find('\0')==std::string::npos;
      if(!valid) { const uint32_t error[4]={0x45474545u,1,0,0}; write_all(fd,error,sizeof(error)); close(fd); continue; }
      const auto generated=std::chrono::steady_clock::now();
      SherpaOnnxGenerationConfig g{}; g.speed=speed; g.sid=0; g.silence_scale=0.2f;
      std::unique_ptr<const SherpaOnnxGeneratedAudio, decltype(&SherpaOnnxDestroyOfflineTtsGeneratedAudio)> audio(
        SherpaOnnxOfflineTtsGenerateWithConfig(tts.get(),text.c_str(),&g,nullptr,nullptr),SherpaOnnxDestroyOfflineTtsGeneratedAudio);
      if(!audio || !audio->samples || audio->n<=0 || audio->n>16777194 || audio->sample_rate<8000 || audio->sample_rate>48000) {
        const uint32_t error[4]={0x45474545u,2,0,0}; write_all(fd,error,sizeof(error)); close(fd); continue;
      }
      std::vector<int16_t> pcm(audio->n);
      for(int32_t i=0;i<audio->n;++i) {
        float sample=audio->samples[i]; if(!std::isfinite(sample)) sample=0;
        pcm[i]=static_cast<int16_t>(std::lround(std::clamp(sample,-1.0f,1.0f)*32767.0f));
      }
      const uint32_t header[4]={0x57474545u,static_cast<uint32_t>(audio->sample_rate),static_cast<uint32_t>(pcm.size()),static_cast<uint32_t>(pcm.size()*2)};
      const bool sent=write_all(fd,header,sizeof(header)) && write_all(fd,pcm.data(),pcm.size()*2);
      std::fprintf(stderr,"EEG_TTS_GENERATED seconds=%.6f samples=%zu rate=%d delivery=%s\n",seconds(generated),pcm.size(),audio->sample_rate,sent?"sent":"client_disconnected");
      std::fflush(stderr); close(fd);
    }
  } catch(const std::exception &e) { std::fprintf(stderr,"EEG_TTS_SERVER_ERROR %s\n",e.what()); return 1; }
}
