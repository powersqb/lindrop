package cn.lindrop.app;

import android.security.keystore.*;
import java.math.BigInteger;
import java.net.Socket;
import java.security.*;
import java.security.cert.X509Certificate;
import java.security.spec.ECGenParameterSpec;
import java.util.Date;
import javax.net.ssl.*;
import javax.security.auth.x500.X500Principal;

/** Device identity stays in Android Keystore; no exportable private-key file. */
public final class Identity {
  public final SSLContext server, client;
  public final String fingerprint;

  public Identity() throws Exception {
    String alias = "lindrop-device-ec-v1";
    KeyStore store = KeyStore.getInstance("AndroidKeyStore");
    store.load(null);
    if (!store.containsAlias(alias)) {
      KeyPairGenerator gen =
          KeyPairGenerator.getInstance(KeyProperties.KEY_ALGORITHM_EC, "AndroidKeyStore");
      gen.initialize(
          new KeyGenParameterSpec.Builder(alias, KeyProperties.PURPOSE_SIGN)
              .setAlgorithmParameterSpec(new ECGenParameterSpec("secp256r1"))
              .setDigests(
                  // Conscrypt hashes the TLS transcript before delegating raw ECDSA
                  // signing to Android Keystore (NONEwithECDSA). The certificate
                  // itself is still signed with SHA-256 or a stronger digest.
                  KeyProperties.DIGEST_NONE,
                  KeyProperties.DIGEST_SHA256,
                  KeyProperties.DIGEST_SHA384,
                  KeyProperties.DIGEST_SHA512)
              .setCertificateSubject(new X500Principal("CN=LinDrop Android"))
              .setCertificateSerialNumber(new BigInteger(Protocol.random(16), 16))
              .setCertificateNotBefore(new Date(1577836800000L))
              .setCertificateNotAfter(new Date(4102444800000L))
              .build());
      gen.generateKeyPair();
    }
    PrivateKey key = (PrivateKey) store.getKey(alias, null);
    X509Certificate cert = (X509Certificate) store.getCertificate(alias);
    cert.verify(cert.getPublicKey()); // A damaged identity is an error, never silently replaced.
    fingerprint = Protocol.sha(cert.getEncoded());
    X509KeyManager keys =
        new X509KeyManager() {
          public String[] getClientAliases(String type, java.security.Principal[] issuers) {
            return new String[] {alias};
          }

          public String chooseClientAlias(
              String[] types, java.security.Principal[] issuers, Socket socket) {
            return alias;
          }

          public String[] getServerAliases(String type, java.security.Principal[] issuers) {
            return "EC".equals(type) ? new String[] {alias} : null;
          }

          public String chooseServerAlias(
              String type, java.security.Principal[] issuers, Socket socket) {
            return "EC".equals(type) ? alias : null;
          }

          public X509Certificate[] getCertificateChain(String name) {
            return new X509Certificate[] {cert};
          }

          public PrivateKey getPrivateKey(String name) {
            return key;
          }
        };
    server = SSLContext.getInstance("TLS");
    server.init(new KeyManager[] {keys}, null, new SecureRandom());
    // Local self-signed certificates are provisional until Wire checks the exact DER pin.
    TrustManager[] provisional = {
      new X509TrustManager() {
        public X509Certificate[] getAcceptedIssuers() {
          return new X509Certificate[0];
        }

        public void checkClientTrusted(X509Certificate[] c, String a) {}

        public void checkServerTrusted(X509Certificate[] c, String a) {}
      }
    };
    client = SSLContext.getInstance("TLS");
    client.init(null, provisional, new SecureRandom());
  }
}
