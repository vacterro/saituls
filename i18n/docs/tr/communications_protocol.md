# Fikir Değerlendirme İletişim Protokolü

Windows bağlam menüsü özelleştirme ekosisteminde çalışan tüm sonraki aracılar, önerilen fikirlere itiraz ederken, bunları değerlendirirken veya geliştirirken bu yapılandırılmış protokole uymak zorundadır.

## 1. "Steel-Man" Doktrini
Bir fikre itiraz etmeden önce, değerlendiren aracı orijinal teklifin en güçlü versiyonunu oluşturmalıdır.
- Temel değer önerisini orijinal yazardan daha net ifade etmek.
- Yaklaşımın en az bir söylenmemiş faydasını belirlemek.

## 2. Red-Teaming (Güvenlik Açığı Değerlendirmesi)
Fikir güçlendirildikten sonra, aracılar aşağıdaki vektörler boyunca ona meydan okumalıdır:
- **Ekosistem Uyumu:** Bu, yerel bir bağlam menüsü aracı gibi mi hissettiriyor yoksa tam bir uygulama olmaya mı çalışıyor?
- **Performans:** 100.000 dosyalık bir dizinde yanlışlıkla çalıştırılırsa ne olur?
- **Yıkıcılık:** Geri döndürülemez veri kaybı riski var mı?
- **Bağımlılık Yükü:** Aşırı dış bağımlılıklar gerektiriyor mu (örneğin devasa Python kütüphaneleri veya yüklenmemiş ikili dosyalar)?

## 3. Çürütme Formatı
Her eleştiri şu şekilde yapılandırılmalıdır:
- **Hipotez:** Fikrin çözmeyi amaçladığı şey.
- **Güvenlik Açığı:** Belirlenen belirli kusur veya risk.
- **Alternatif Formülasyon:** Değeri korurken riski azaltan önerilen bir dönüş.

## 4. Nihai Karar Mekanizması
Fikirler, dosya sistemi için felaket niteliğinde bir risk oluşturmadıkça (örneğin izlenmeyen özyinelemeli silme) bir dönüş önerilmeden tamamen reddedilmemelidir.

<!-- source-digest: communications_protocol.md sha256:5ac43f32d3432e2d -->
