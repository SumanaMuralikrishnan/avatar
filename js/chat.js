// Copyright (c) Microsoft. All rights reserved.
// Licensed under the MIT license.

// Global objects
var speechRecognizer;
var avatarSynthesizer;
var peerConnection;
var peerConnectionDataChannel;
var messages = [];
var messageInitiated = false;
var sentenceLevelPunctuations = ['.', '?', '!', ':', ';', '。', '？', '！', '：', '；'];
var enableDisplayTextAlignmentWithSpeech = true;
var isSpeaking = false;
var isReconnecting = false;
var speakingText = "";
var spokenTextQueue = [];
var repeatSpeakingSentenceAfterReconnection = true;
var sessionActive = false;
var userClosedSession = false;
var lastInteractionTime = new Date();
var lastSpeakTime;
var pendingQueries = [];
var config;

// Load config async (replace with your config file logic)
async function loadConfig() {
  console.log("Loading configuration...");
  try {
    // Placeholder: Replace with fetch('config.json')
    config = await Promise.resolve({
      cogSvcRegion: "",
      cogSvcSubKey: "",
      talkingAvatarCharacter: "lisa",
      talkingAvatarStyle: "casual-sitting",
      ttsVoice: "en-US-JennyNeural",
      sttLocales: ["en-US"],
      systemPrompt: "You are a helpful assistant."
    });
    console.log("Configuration loaded:", config);
  } catch (error) {
    console.error("Failed to load config:", error);
    alert("Failed to load configuration. Check console.");
  }
}

// Verify Azure Speech SDK
function checkSpeechSDK() {
  console.log("Checking Azure Speech SDK...");
  if (typeof SpeechSDK === 'undefined') {
    console.error("Azure Speech SDK not loaded.");
    alert("Failed to load Azure Speech SDK. Check network or browser.");
    return false;
  }
  console.log("Azure Speech SDK loaded.");
  return true;
}

// Connect to avatar service
async function connectAvatar() {
  console.log("Starting avatar session...");
  document.getElementById('startSession').innerHTML = "Starting...";
  document.getElementById('startSession').disabled = true;
  document.getElementById('chatHistory').innerHTML = '<div class="system-message"><span>Session starting...</span></div>';
  document.getElementById('chatHistory').hidden = false;

  if (!config) {
    await loadConfig();
  }

  if (!checkSpeechSDK()) {
    document.getElementById('startSession').innerHTML = "Start Session";
    document.getElementById('startSession').disabled = false;
    document.getElementById('chatHistory').innerHTML = '<div class="system-message"><span>Failed to start session. Check console.</span></div>';
    return;
  }

  try {
    const speechSynthesisConfig = SpeechSDK.SpeechConfig.fromSubscription(config.cogSvcSubKey, config.cogSvcRegion);
    const avatarConfig = new SpeechSDK.AvatarConfig(config.talkingAvatarCharacter, config.talkingAvatarStyle);
    avatarSynthesizer = new SpeechSDK.AvatarSynthesizer(speechSynthesisConfig, avatarConfig);
    avatarSynthesizer.avatarEventReceived = function (s, e) {
      console.log(`Event received: ${e.description}, offset: ${e.offset / 10000}ms`);
    };

    const speechRecognitionConfig = SpeechSDK.SpeechConfig.fromEndpoint(
      new URL(`wss://${config.cogSvcRegion}.stt.speech.microsoft.com/speech/universal/v2`),
      config.cogSvcSubKey
    );
    speechRecognitionConfig.setProperty(SpeechSDK.PropertyId.SpeechServiceConnection_LanguageIdMode, "Continuous");
    const autoDetectSourceLanguageConfig = SpeechSDK.AutoDetectSourceLanguageConfig.fromLanguages(config.sttLocales);
    speechRecognizer = SpeechSDK.SpeechRecognizer.FromConfig(
      speechRecognitionConfig,
      autoDetectSourceLanguageConfig,
      SpeechSDK.AudioConfig.fromDefaultMicrophoneInput()
    );

    if (!messageInitiated) {
      initMessages();
      messageInitiated = true;
    }

    const xhr = new XMLHttpRequest();
    xhr.open("GET", `https://${config.cogSvcRegion}.tts.speech.microsoft.com/cognitiveservices/avatar/relay/token/v1`);
    xhr.setRequestHeader("Ocp-Apim-Subscription-Key", config.cogSvcSubKey);
    xhr.addEventListener("readystatechange", function () {
      if (this.readyState === 4) {
        if (this.status === 200) {
          console.log("WebRTC token fetched.");
          const responseData = JSON.parse(this.responseText);
          setupWebRTC(responseData.Urls[0], responseData.Username, responseData.Password);
        } else {
          console.error(`Failed to fetch WebRTC token: ${this.status}`);
          alert(`Failed to connect to avatar service. Status: ${this.status}. Check credentials.`);
          document.getElementById('startSession').innerHTML = "Start Session";
          document.getElementById('startSession').disabled = false;
          document.getElementById('chatHistory').innerHTML = '<div class="system-message"><span>Failed to start session. Check console.</span></div>';
          avatarSynthesizer = null;
        }
      }
    });
    xhr.send();
  } catch (error) {
    console.error("Error initializing avatar:", error);
    alert("Failed to initialize avatar. Check console.");
    document.getElementById('startSession').innerHTML = "Start Session";
    document.getElementById('startSession').disabled = false;
    document.getElementById('chatHistory').innerHTML = '<div class="system-message"><span>Failed to start session. Check console.</span></div>';
  }
}

// Disconnect from avatar service
function disconnectAvatar() {
  console.log("Disconnecting avatar session...");
  if (avatarSynthesizer) {
    avatarSynthesizer.close();
    avatarSynthesizer = null;
  }
  if (speechRecognizer) {
    speechRecognizer.stopContinuousRecognitionAsync();
    speechRecognizer.close();
    speechRecognizer = null;
  }
  if (peerConnection) {
    peerConnection.close();
    peerConnection = null;
  }
  sessionActive = false;
  userClosedSession = true;
  pendingQueries = [];
  document.getElementById('microphone').disabled = true;
  document.getElementById('stopSession').disabled = true;
  document.getElementById('userMessageBox').disabled = true;
  document.getElementById('chatHistory').hidden = true;
  document.getElementById('startSession').innerHTML = "Start Session";
  document.getElementById('startSession').disabled = false;
}

// Setup WebRTC
function setupWebRTC(iceServerUrl, iceServerUsername, iceServerCredential) {
  console.log("Setting up WebRTC...");
  peerConnection = new RTCPeerConnection({
    iceServers: [{ urls: [iceServerUrl], username: iceServerUsername, credential: iceServerCredential }]
  });

  peerConnection.ontrack = function (event) {
    if (event.track.kind === 'audio') {
      let audioElement = document.createElement('audio');
      audioElement.id = 'audioPlayer';
      audioElement.srcObject = event.streams[0];
      audioElement.autoplay = false;
      audioElement.addEventListener('loadeddata', () => audioElement.play());
      audioElement.onplaying = () => console.log(`WebRTC ${event.track.kind} channel connected.`);
      let remoteVideoDiv = document.getElementById('remoteVideo');
      for (let i = 0; i < remoteVideoDiv.childNodes.length; i++) {
        if (remoteVideoDiv.childNodes[i].localName === event.track.kind) {
          remoteVideoDiv.removeChild(remoteVideoDiv.childNodes[i]);
        }
      }
      remoteVideoDiv.appendChild(audioElement);
    }

    if (event.track.kind === 'video') {
      let videoElement = document.createElement('video');
      videoElement.id = 'videoPlayer';
      videoElement.srcObject = event.streams[0];
      videoElement.autoplay = false;
      videoElement.addEventListener('loadeddata', () => videoElement.play());
      videoElement.playsInline = true;
      videoElement.style.width = '640px';
      document.getElementById('remoteVideo').appendChild(videoElement);

      videoElement.onplaying = () => {
        let remoteVideoDiv = document.getElementById('remoteVideo');
        for (let i = 0; i < remoteVideoDiv.childNodes.length; i++) {
          if (remoteVideoDiv.childNodes[i].localName === event.track.kind) {
            remoteVideoDiv.removeChild(remoteVideoDiv.childNodes[i]);
          }
        }
        videoElement.style.width = '640px';
        remoteVideoDiv.appendChild(videoElement);
        console.log(`WebRTC ${event.track.kind} channel connected.`);
        document.getElementById('microphone').disabled = false;
        document.getElementById('stopSession').disabled = false;
        document.getElementById('userMessageBox').disabled = false;
        document.getElementById('chatHistory').innerHTML = ''; // Clear "Session starting..."
        document.getElementById('chatHistory').hidden = false;
        isReconnecting = false;
        setTimeout(() => {
          sessionActive = true;
          console.log("Session active, processing pending queries:", pendingQueries);
          while (pendingQueries.length > 0) {
            handleUserQuery(pendingQueries.shift());
          }
        }, 300); // Reduced to 300ms
      };
    }
  };

  peerConnection.addEventListener("datachannel", event => {
    peerConnectionDataChannel = event.channel;
    peerConnectionDataChannel.onmessage = e => {
      console.log(`[${(new Date()).toISOString()}] WebRTC event: ${e.data}`);
    };
  });

  peerConnection.createDataChannel("eventChannel");
  peerConnection.oniceconnectionstatechange = () => {
    console.log(`WebRTC status: ${peerConnection.iceConnectionState}`);
  };

  peerConnection.addTransceiver('video', { direction: 'sendrecv' });
  peerConnection.addTransceiver('audio', { direction: 'sendrecv' });

  avatarSynthesizer.startAvatarAsync(peerConnection).then((r) => {
    if (r.reason === SpeechSDK.ResultReason.SynthesizingAudioCompleted) {
      console.log(`[${(new Date()).toISOString()}] Avatar started. Result ID: ${r.resultId}`);
    } else {
      console.log(`[${(new Date()).toISOString()}] Unable to start avatar. Result ID: ${r.resultId}`);
      document.getElementById('startSession').innerHTML = "Start Session";
      document.getElementById('startSession').disabled = false;
      document.getElementById('chatHistory').innerHTML = '<div class="system-message"><span>Failed to start session. Check console.</span></div>';
    }
  }).catch((error) => {
    console.error(`[${(new Date()).toISOString()}] Avatar failed to start: ${error}`);
    alert("Failed to start avatar. Check console.");
    document.getElementById('startSession').innerHTML = "Start Session";
    document.getElementById('startSession').disabled = false;
    document.getElementById('chatHistory').innerHTML = '<div class="system-message"><span>Failed to start session. Check console.</span></div>';
  });
}

// Initialize messages
function initMessages() {
  messages = [{
    role: 'system',
    content: config.systemPrompt
  }];
}

// HTML encode text
function htmlEncode(text) {
  const entityMap = {
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
    '/': '&#x2F;'
  };
  return String(text).replace(/[&<>"'\/]/g, match => entityMap[match]);
}

// Speak text
function speak(text, endingSilenceMs = 0) {
  if (isSpeaking) {
    spokenTextQueue.push(text);
    return;
  }
  speakNext(text, endingSilenceMs);
}

function speakNext(text, endingSilenceMs = 0, skipUpdatingChatHistory = false) {
  let ssml = `<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xmlns:mstts='http://www.w3.org/2001/mstts' xml:lang='en-US'><voice name='${config.ttsVoice}'><mstts:leadingsilence-exact value='0'/>${htmlEncode(text)}</voice></speak>`;
  if (endingSilenceMs > 0) {
    ssml = `<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xmlns:mstts='http://www.w3.org/2001/mstts' xml:lang='en-US'><voice name='${config.ttsVoice}'><mstts:leadingsilence-exact value='0'/>${htmlEncode(text)}<break time='${endingSilenceMs}ms' /></voice></speak>`;
  }

  if (enableDisplayTextAlignmentWithSpeech && !skipUpdatingChatHistory) {
    let chatHistoryTextArea = document.getElementById('chatHistory');
    chatHistoryTextArea.innerHTML += `<div class="assistant-message"><span>${text.replace(/\n/g, '<br/>')}</span></div>`;
    chatHistoryTextArea.scrollTop = chatHistoryTextArea.scrollHeight;
  }

  lastSpeakTime = new Date();
  isSpeaking = true;
  speakingText = text;
  document.getElementById('stopSpeaking').disabled = false;
  avatarSynthesizer.speakSsmlAsync(ssml).then(
    (result) => {
      if (result.reason === SpeechSDK.ResultReason.SynthesizingAudioCompleted) {
        console.log(`Speech synthesized for text [${text}]. Result ID: ${result.resultId}`);
        lastSpeakTime = new Date();
      } else {
        console.log(`Error speaking SSML. Result ID: ${result.resultId}`);
      }
      speakingText = '';
      if (spokenTextQueue.length > 0) {
        speakNext(spokenTextQueue.shift());
      } else {
        isSpeaking = false;
        document.getElementById('stopSpeaking').disabled = true;
      }
    }).catch((error) => {
      console.error(`Error speaking SSML: ${error}`);
      speakingText = '';
      if (spokenTextQueue.length > 0) {
        speakNext(spokenTextQueue.shift());
      } else {
        isSpeaking = false;
        document.getElementById('stopSpeaking').disabled = true;
      }
    });
}

function stopSpeaking() {
  lastInteractionTime = new Date();
  spokenTextQueue = [];
  avatarSynthesizer.stopSpeakingAsync().then(() => {
    isSpeaking = false;
    document.getElementById('stopSpeaking').disabled = true;
    console.log(`[${(new Date()).toISOString()}] Stop speaking request sent.`);
  }).catch((error) => {
    console.error(`Error stopping speaking: ${error}`);
  });
}

function handleUserQuery(userQuery) {
  console.log("Handling user query:", userQuery);
  if (!sessionActive) {
    console.log("Session not active, queuing query:", userQuery);
    pendingQueries.push(userQuery);
    let chatHistoryTextArea = document.getElementById('chatHistory');
    chatHistoryTextArea.innerHTML = '<div class="system-message"><span>Session starting, query queued...</span></div>';
    return;
  }

  lastInteractionTime = new Date();
  let chatMessage = {
    role: 'user',
    content: userQuery
  };
  messages.push(chatMessage);

  let chatHistoryTextArea = document.getElementById('chatHistory');
  chatHistoryTextArea.innerHTML += `<div class="user-message"><span>${htmlEncode(userQuery)}</span></div>`;
  chatHistoryTextArea.scrollTop = chatHistoryTextArea.scrollHeight;

  if (isSpeaking) {
    stopSpeaking();
  }

  console.log("Sending request to /ask_agent...");
  fetch("https://avatar-v4ja.onrender.com/ask_agent", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      session_id: "demo-session",
      message: userQuery
    })
  })
  .then(response => {
    console.log("Received /ask_agent response, status:", response.status);
    if (!response.ok) {
      return response.text().then(text => {
        throw new Error(`HTTP ${response.status}: ${text}`);
      });
    }
    return response.json();
  })
  .then(data => {
    console.log("Parsed /ask_agent response:", data);
    const assistantReply = data.text;
    if (!assistantReply) {
      console.error("Empty response from /ask_agent.");
      return;
    }
    const transcriptionDiv = document.getElementById("transcriptionText");
    transcriptionDiv.innerHTML += `<div><b>Agent:</b> ${htmlEncode(assistantReply)}<br></div><br>`;
    transcriptionDiv.scrollTop = transcriptionDiv.scrollHeight;

    let assistantMessage = {
      role: 'assistant',
      content: assistantReply 
    };
    messages.push(assistantMessage);

    let spokenSentence = '';
    let displaySentence = '';
    const tokens = assistantReply.split(/([.!?;:。？！：；])/);
    for (let i = 0; i < tokens.length; i++) {
      const token = tokens[i];
      displaySentence += token;
      spokenSentence += token;
      if (sentenceLevelPunctuations.includes(token)) {
        if (spokenSentence.trim()) {
          speak(spokenSentence);
          spokenSentence = '';
        }
        if (!enableDisplayTextAlignmentWithSpeech) {
          chatHistoryTextArea.innerHTML += `<div class="assistant-message"><span>${displaySentence.replace(/\n/g, '<br/>')}</span></div>`;
          chatHistoryTextArea.scrollTop = chatHistoryTextArea.scrollHeight;
          displaySentence = '';
        }
      }
    }

    if (spokenSentence.trim()) {
      speak(spokenSentence);
    }
    if (!enableDisplayTextAlignmentWithSpeech && displaySentence) {
      chatHistoryTextArea.innerHTML += `<div class="assistant-message"><span>${displaySentence.replace(/\n/g, '<br/>')}</span></div>`;
      chatHistoryTextArea.scrollTop = chatHistoryTextArea.scrollHeight;
    }
  })
  .catch(err => {
    console.error("Error from /ask_agent:", err);
    alert(`Failed to get response: ${err.message}`);
    chatHistoryTextArea.innerHTML += `<div class="system-message"><span>Error: ${htmlEncode(err.message)}</span></div>`;
    chatHistoryTextArea.scrollTop = chatHistoryTextArea.scrollHeight;
  });
}

function checkHung() {
  let videoElement = document.getElementById('videoPlayer');
  if (videoElement && sessionActive) {
    let videoTime = videoElement.currentTime;
    setTimeout(() => {
      if (videoElement.currentTime === videoTime && sessionActive) {
        sessionActive = false;
        console.log(`[${(new Date()).toISOString()}] Video stream disconnected, reconnecting...`);
        isReconnecting = true;
        if (peerConnectionDataChannel) {
          peerConnectionDataChannel.onmessage = null;
        }
        if (avatarSynthesizer) {
          avatarSynthesizer.close();
        }
        connectAvatar();
      }
    }, 2000);
  }
}

function toggleChat() {
  // const panel = document.getElementById("chatHistoryPanel");
  // const toggleBtn = document.getElementById("toggleChat");

  // if (panel.style.display === "none" || panel.style.display === "") {
  //   panel.style.display = "block";
  //   toggleBtn.textContent = "📝 Hide Transcriptions";
  // } else {
  //   panel.style.display = "none";
  //   toggleBtn.textContent = "📝 Show Transcriptions";
  // }
  const panel = document.getElementById("chatHistoryPanel");
 const toggleBtn = document.getElementById("toggleChat");
 if (panel.style.display === "none" || panel.style.display === "") {
   panel.style.display = "block";
   toggleBtn.textContent = "📝 Hide Transcriptions";
 } else {
   panel.style.display = "none";
   toggleBtn.textContent = "📝 Show Transcriptions";
 }
}



function showLiveCaption(text) {
  const captionDiv = document.getElementById("liveCaption");
  captionDiv.textContent = text;
  captionDiv.hidden = false;

  clearTimeout(captionDiv._hideTimeout);
  captionDiv._hideTimeout = setTimeout(() => {
    captionDiv.hidden = true;
  }, 4000);
}


window.onload = async () => {
  await loadConfig();
  setInterval(checkHung, 2000);
  document.getElementById('userMessageBox').addEventListener('keyup', (e) => {
 if (e.key === 'Enter') {
   const userQuery = document.getElementById('userMessageBox').value.trim();
   if (userQuery) {
     // append USER typed text into transcription panel
     const transcriptionDiv = document.getElementById("transcriptionText");
     transcriptionDiv.innerHTML += `<div><b>User:</b> ${htmlEncode(userQuery)}<br></div><br>`;
     transcriptionDiv.scrollTop = transcriptionDiv.scrollHeight;
     handleUserQuery(userQuery);
     document.getElementById('userMessageBox').value = '';
   }
 }
});
};

window.startSession = () => {
  lastInteractionTime = new Date();
  userClosedSession = false;
  connectAvatar();
};

window.stopSession = () => {
  lastInteractionTime = new Date();
  document.getElementById('microphone').disabled = true;
  document.getElementById('stopSession').disabled = true;
  document.getElementById('userMessageBox').disabled = true;
  document.getElementById('chatHistory').hidden = true;
  document.getElementById('startSession').innerHTML = "Start Session";
  document.getElementById('startSession').disabled = false;
  userClosedSession = true;
  disconnectAvatar();
};

// window.microphone = () => {
//   lastInteractionTime = new Date();
//   if (document.getElementById('microphone').innerHTML === 'Stop Microphone') {
//     speechRecognizer.stopContinuousRecognitionAsync(() => {
//       document.getElementById('microphone').innerHTML = 'Start Microphone';
//       document.getElementById('microphone').disabled = false;
//     }, (err) => {
//       console.error("Failed to stop recognition:", err);
//       document.getElementById('microphone').disabled = false;
//     });
//     return;
//   }

//   document.getElementById('microphone').disabled = true;
//   speechRecognizer.startContinuousRecognitionAsync(() => {
//     document.getElementById('microphone').innerHTML = 'Stop Microphone';
//     document.getElementById('microphone').disabled = false;
//   }, (err) => {
//     console.error("Failed to start recognition:", err);
//     document.getElementById('microphone').disabled = false;
//   });

//   speechRecognizer.recognized = async (s, e) => {
//     if (e.result.reason === SpeechSDK.ResultReason.RecognizedSpeech) {
//       let userQuery = e.result.text.trim();
//       if (userQuery) {
//         handleUserQuery(userQuery);
//       }
//     }
//   };
// };
// Toggle transcription panel
window.microphone = () => {
  lastInteractionTime = new Date();

  const micButton = document.getElementById('microphone');

  if (micButton.innerHTML === 'Stop Microphone') {
    // Stop microphone
    speechRecognizer.stopContinuousRecognitionAsync(() => {
      micButton.innerHTML = '🎤 Mic';
      micButton.disabled = false;
    }, (err) => {
      console.error("Failed to stop recognition:", err);
      micButton.disabled = false;
    });
    return;
  }

  micButton.disabled = true;

  // Start continuous recognition
  speechRecognizer.startContinuousRecognitionAsync(() => {
    micButton.innerHTML = 'Stop Microphone';
    micButton.disabled = false;
  }, (err) => {
    console.error("Failed to start recognition:", err);
    micButton.disabled = false;
  });

  // On recognized (final speech result)
  speechRecognizer.recognized = async (s, e) => {
    if (e.result.reason === SpeechSDK.ResultReason.RecognizedSpeech) {
      let userQuery = e.result.text.trim();
      if (userQuery) {
        // ✅ AUTO-STOP avatar speech when user speaks
        if (isSpeaking) {
          console.log("User started speaking - stopping avatar speech...");
          stopSpeaking();
        }

        // Append user text to transcription panel
        const transcriptionDiv = document.getElementById("transcriptionText");
        transcriptionDiv.innerHTML += `<div><b>User:</b> ${htmlEncode(userQuery)}<br></div><br>`;
        transcriptionDiv.scrollTop = transcriptionDiv.scrollHeight;

        // Send to agent
        handleUserQuery(userQuery);
      }
    }
  };
};


function toggleChat() {

  const panel = document.getElementById("chatHistoryPanel");

  const toggleBtn = document.getElementById("toggleChat");

  if (panel.style.display === "none" || panel.style.display === "") {

    panel.style.display = "block";

    toggleBtn.textContent = "📝 Hide Transcriptions";

  } else {

    panel.style.display = "none";

    toggleBtn.textContent = "📝 Show Transcriptions";

  }

}

window.microphone = () => {

  lastInteractionTime = new Date();

  if (document.getElementById('microphone').innerHTML === 'Stop Microphone') {

    // Stop microphone

    speechRecognizer.stopContinuousRecognitionAsync(() => {

      document.getElementById('microphone').innerHTML = '🎤 Mic';

      document.getElementById('microphone').disabled = false;

    }, (err) => {

      console.error("Failed to stop recognition:", err);

      document.getElementById('microphone').disabled = false;

    });

    return;

  }

  document.getElementById('microphone').disabled = true;

  // Start continuous recognition

  speechRecognizer.startContinuousRecognitionAsync(() => {

    document.getElementById('microphone').innerHTML = 'Stop Microphone';

    document.getElementById('microphone').disabled = false;

  }, (err) => {

    console.error("Failed to start recognition:", err);

    document.getElementById('microphone').disabled = false;

  });

  speechRecognizer.recognized = async (s, e) => {

    if (e.result.reason === SpeechSDK.ResultReason.RecognizedSpeech) {

      let userQuery = e.result.text.trim();

      if (userQuery) {

        // 👉 append the transcription

        const transcriptionDiv = document.getElementById("transcriptionText");

        transcriptionDiv.innerHTML += `<div><b>User:</b>${htmlEncode(userQuery)}<br></div><br>`;

        transcriptionDiv.scrollTop = transcriptionDiv.scrollHeight;

        // send to agent

        handleUserQuery(userQuery);

      }

    }

  };

};
 
window.stopSpeaking = stopSpeaking;
