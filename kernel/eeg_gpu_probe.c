/* SPDX-License-Identifier: MIT
 * Render a known GLES2 triangle on an offscreen GBM device and verify pixels.
 * Uses the render node, never takes the display DRM master or starts EEG.
 */
#define _GNU_SOURCE
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <GLES2/gl2.h>
#include <gbm.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static GLuint compile(GLenum type, const char *source)
{
    GLuint shader = glCreateShader(type);
    GLint ok = GL_FALSE;
    char log[1024];
    glShaderSource(shader, 1, &source, NULL);
    glCompileShader(shader);
    glGetShaderiv(shader, GL_COMPILE_STATUS, &ok);
    if (!ok) {
        glGetShaderInfoLog(shader, sizeof(log), NULL, log);
        fprintf(stderr, "shader compilation failed: %s\n", log);
        glDeleteShader(shader);
        return 0;
    }
    return shader;
}

int main(int argc, char **argv)
{
    const char *node = argc > 1 ? argv[1] : "/dev/dri/renderD128";
    const char *vertex = "attribute vec2 position; void main(){gl_Position=vec4(position,0.0,1.0);}";
    const char *fragment = "precision mediump float; void main(){gl_FragColor=vec4(1.0,0.0,0.0,1.0);}";
    const EGLint config_attrs[] = {EGL_RENDERABLE_TYPE, EGL_OPENGL_ES2_BIT, EGL_RED_SIZE, 8, EGL_GREEN_SIZE, 8,
        EGL_BLUE_SIZE, 8, EGL_ALPHA_SIZE, 8, EGL_NONE};
    const EGLint context_attrs[] = {EGL_CONTEXT_CLIENT_VERSION, 2, EGL_NONE};
    const GLfloat vertices[] = {-1.0f,-1.0f, 1.0f,-1.0f, 0.0f,1.0f};
    unsigned char center[4] = {0}, corner[4] = {0};
    EGLint major = 0, minor = 0, count = 0;
    EGLDisplay display = EGL_NO_DISPLAY;
    EGLContext context = EGL_NO_CONTEXT;
    EGLConfig config;
    GLuint program = 0, vs = 0, fs = 0, framebuffer = 0, colorbuffer = 0;
    GLint linked = GL_FALSE;
    int passed = 0, fd = -1;
    struct gbm_device *gbm = NULL;
    PFNEGLGETPLATFORMDISPLAYEXTPROC get_platform;
    fd = open(node, O_RDWR | O_CLOEXEC);
    if (fd < 0) { perror(node); goto done; }
    gbm = gbm_create_device(fd);
    if (!gbm) { fprintf(stderr, "gbm_create_device failed\n"); goto done; }
    get_platform = (PFNEGLGETPLATFORMDISPLAYEXTPROC) eglGetProcAddress("eglGetPlatformDisplayEXT");
    if (!get_platform) { fprintf(stderr, "EGL_EXT_platform_base missing\n"); goto done; }
    display = get_platform(EGL_PLATFORM_GBM_KHR, gbm, NULL);
    if (display == EGL_NO_DISPLAY || !eglInitialize(display, &major, &minor) ||
        !eglBindAPI(EGL_OPENGL_ES_API) ||
        !eglChooseConfig(display, config_attrs, &config, 1, &count) || count != 1) {
        fprintf(stderr, "EGL initialization/config failed: 0x%x\n", eglGetError()); goto done;
    }
    if (!strstr(eglQueryString(display, EGL_EXTENSIONS), "EGL_KHR_surfaceless_context")) {
        fprintf(stderr, "EGL_KHR_surfaceless_context required\n"); goto done;
    }
    context = eglCreateContext(display, config, EGL_NO_CONTEXT, context_attrs);
    if (context == EGL_NO_CONTEXT ||
        !eglMakeCurrent(display, EGL_NO_SURFACE, EGL_NO_SURFACE, context)) {
        fprintf(stderr, "EGL surfaceless context failed: 0x%x\n", eglGetError()); goto done;
    }
    printf("EGL_VERSION=%d.%d\nEGL_VENDOR=%s\nGL_VENDOR=%s\nGL_RENDERER=%s\nGL_VERSION=%s\n",
           major, minor, eglQueryString(display, EGL_VENDOR), glGetString(GL_VENDOR),
           glGetString(GL_RENDERER), glGetString(GL_VERSION));
    if (!strstr((const char *)glGetString(GL_RENDERER), "Mali") &&
        !strstr((const char *)glGetString(GL_RENDERER), "lima")) {
        fprintf(stderr, "hardware Mali/Lima renderer required\n"); goto done;
    }
    glGenFramebuffers(1, &framebuffer); glBindFramebuffer(GL_FRAMEBUFFER, framebuffer);
    glGenRenderbuffers(1, &colorbuffer); glBindRenderbuffer(GL_RENDERBUFFER, colorbuffer);
    glRenderbufferStorage(GL_RENDERBUFFER, GL_RGBA4, 64, 64);
    glFramebufferRenderbuffer(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_RENDERBUFFER, colorbuffer);
    if (glCheckFramebufferStatus(GL_FRAMEBUFFER) != GL_FRAMEBUFFER_COMPLETE) {
        fprintf(stderr, "GLES2 offscreen framebuffer incomplete\n"); goto done;
    }
    vs = compile(GL_VERTEX_SHADER, vertex); fs = compile(GL_FRAGMENT_SHADER, fragment);
    if (!vs || !fs) goto done;
    program = glCreateProgram();
    glAttachShader(program, vs); glAttachShader(program, fs);
    glBindAttribLocation(program, 0, "position"); glLinkProgram(program);
    glGetProgramiv(program, GL_LINK_STATUS, &linked);
    if (!linked) { fprintf(stderr, "GLES2 program link failed\n"); goto done; }
    glUseProgram(program); glViewport(0, 0, 64, 64);
    glClearColor(0.0f, 0.0f, 1.0f, 1.0f); glClear(GL_COLOR_BUFFER_BIT);
    glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 0, vertices);
    glEnableVertexAttribArray(0); glDrawArrays(GL_TRIANGLES, 0, 3); glFinish();
    glReadPixels(32, 32, 1, 1, GL_RGBA, GL_UNSIGNED_BYTE, center);
    glReadPixels(1, 62, 1, 1, GL_RGBA, GL_UNSIGNED_BYTE, corner);
    passed = glGetError() == GL_NO_ERROR && center[0] > 240 && center[1] < 16 &&
        center[2] < 16 && corner[0] < 16 && corner[1] < 16 && corner[2] > 240;
    printf("CENTER_RGBA=%u,%u,%u,%u\nCORNER_RGBA=%u,%u,%u,%u\nEEG_GPU_RENDER_%s\n",
           center[0], center[1], center[2], center[3], corner[0], corner[1], corner[2], corner[3],
           passed ? "PASS" : "FAIL");
done:
    if (colorbuffer) glDeleteRenderbuffers(1, &colorbuffer);
    if (framebuffer) glDeleteFramebuffers(1, &framebuffer);
    if (program) glDeleteProgram(program);
    if (vs) glDeleteShader(vs);
    if (fs) glDeleteShader(fs);
    if (display != EGL_NO_DISPLAY) {
        eglMakeCurrent(display, EGL_NO_SURFACE, EGL_NO_SURFACE, EGL_NO_CONTEXT);
        if (context != EGL_NO_CONTEXT) eglDestroyContext(display, context);
        eglTerminate(display);
    }
    if (gbm) gbm_device_destroy(gbm);
    if (fd >= 0) close(fd);
    return passed ? EXIT_SUCCESS : EXIT_FAILURE;
}
