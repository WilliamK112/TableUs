"use client";

import { useState, useRef } from "react";
import { Camera, Upload, Sparkles, Star, Loader2, CheckCircle } from "lucide-react";
import { motion, AnimatePresence } from "framer-motion";
import { useUser } from "../context/user-context";
import { api, apiFormData } from "../lib/api";

type FoodAnalysis = { dish: string; cuisine: string; description: string; flavor_tags?: string[] };

export default function ReviewPage() {
  const { currentUser } = useUser();
  const fileRef = useRef<HTMLInputElement>(null);

  const [imagePreview, setImagePreview] = useState<string | null>(null);
  const [analysis, setAnalysis] = useState<FoodAnalysis | null>(null);
  const [analyzing, setAnalyzing] = useState(false);

  const [restaurantName, setRestaurantName] = useState("");
  const [reviewText, setReviewText] = useState("");
  const [rating, setRating] = useState(4);
  const [submitting, setSubmitting] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [updatedProfile, setUpdatedProfile] = useState("");

  const handleFileSelect = async (file: File) => {
    setImagePreview(URL.createObjectURL(file));
    setAnalysis(null);

    setAnalyzing(true);
    try {
      const form = new FormData();
      form.append("image", file);
      const result = await apiFormData<FoodAnalysis>("/api/food/analyze", form);
      setAnalysis(result);
      if (!restaurantName && result.cuisine) {
        setRestaurantName("");
      }
    } catch {
      setAnalysis({ dish: "Could not analyze", cuisine: "Unknown", description: "Try another photo" });
    } finally {
      setAnalyzing(false);
    }
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    const file = e.dataTransfer.files[0];
    if (file?.type.startsWith("image/")) handleFileSelect(file);
  };

  const handleSubmit = async () => {
    if (!currentUser || !reviewText.trim()) return;
    setSubmitting(true);
    try {
      const result = await api<{ review: unknown; updated_taste_profile: string }>("/api/reviews/submit", {
        method: "POST",
        body: JSON.stringify({
          user_id: currentUser.id,
          restaurant_name: restaurantName || analysis?.dish || "Unknown",
          review_text: reviewText,
          rating,
          dish: analysis?.dish,
          cuisine: analysis?.cuisine,
        }),
      });
      setUpdatedProfile(result.updated_taste_profile);
      setSubmitted(true);
    } catch {
      // ignore
    } finally {
      setSubmitting(false);
    }
  };

  const reset = () => {
    setImagePreview(null);
    setAnalysis(null);
    setRestaurantName("");
    setReviewText("");
    setRating(4);
    setSubmitted(false);
    setUpdatedProfile("");
  };

  return (
    <div className="h-full overflow-y-auto">
      <div className="max-w-2xl mx-auto px-8 py-8">
        <motion.div initial={{ opacity: 0, y: -10 }} animate={{ opacity: 1, y: 0 }}>
          <h1 className="text-3xl font-bold">Review</h1>
          <p className="text-[var(--muted-foreground)] text-sm mt-1">
            Snap a food photo, write your thoughts, and your taste profile evolves.
          </p>
        </motion.div>

        <AnimatePresence mode="wait">
          {submitted ? (
            <motion.div key="success" initial={{ opacity: 0, scale: 0.95 }} animate={{ opacity: 1, scale: 1 }}
              className="mt-8 space-y-6">
              <div className="glass rounded-2xl p-6 text-center">
                <CheckCircle className="w-12 h-12 text-green-400 mx-auto mb-3" />
                <h2 className="text-xl font-semibold mb-1">Review Submitted!</h2>
                <p className="text-sm text-[var(--muted-foreground)]">Your taste profile has been updated.</p>
              </div>

              {updatedProfile && (
                <div className="glass rounded-2xl p-5">
                  <div className="flex items-center gap-2 mb-3">
                    <Sparkles className="w-4 h-4 text-[var(--accent-light)]" />
                    <h3 className="text-sm font-semibold">Updated Taste Profile</h3>
                  </div>
                  <div className="text-sm text-[var(--accent-light)] leading-relaxed space-y-1">
                    {updatedProfile.includes("•")
                      ? updatedProfile.split("•").filter(Boolean).map((line, i) => (
                          <div key={i} className="flex gap-2">
                            <span className="shrink-0">•</span>
                            <span>{line.trim()}</span>
                          </div>
                        ))
                      : <p>{updatedProfile}</p>
                    }
                  </div>
                </div>
              )}

              <button onClick={reset}
                className="w-full py-3 rounded-xl bg-[var(--accent)] text-white font-medium hover:bg-[var(--accent)]/80 transition-colors">
                Write Another Review
              </button>
            </motion.div>
          ) : (
            <motion.div key="form" initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="mt-8 space-y-6">
              {/* Photo upload */}
              <div
                onClick={() => fileRef.current?.click()}
                onDragOver={(e) => e.preventDefault()}
                onDrop={handleDrop}
                className="glass rounded-2xl overflow-hidden cursor-pointer hover:border-[var(--accent)]/40 transition-colors"
              >
                {imagePreview ? (
                  <div className="relative">
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img src={imagePreview} alt="Food" className="w-full h-64 object-cover" />
                    {analyzing && (
                      <div className="absolute inset-0 bg-black/60 flex items-center justify-center">
                        <div className="text-center">
                          <Loader2 className="w-8 h-8 animate-spin text-[var(--accent-light)] mx-auto mb-2" />
                          <p className="text-sm">Gemini is analyzing your food...</p>
                        </div>
                      </div>
                    )}
                  </div>
                ) : (
                  <div className="flex flex-col items-center justify-center py-16 gap-3">
                    <div className="w-14 h-14 rounded-2xl bg-[var(--accent)]/10 flex items-center justify-center">
                      <Camera className="w-7 h-7 text-[var(--accent)]/60" />
                    </div>
                    <div className="text-center">
                      <p className="text-sm font-medium">Upload a food photo</p>
                      <p className="text-xs text-[var(--muted-foreground)] mt-0.5">Click or drag & drop</p>
                    </div>
                    <div className="flex items-center gap-2 text-xs text-[var(--muted-foreground)]">
                      <Upload className="w-3 h-3" />
                      <span>JPG, PNG, WebP</span>
                    </div>
                  </div>
                )}
                <input ref={fileRef} type="file" accept="image/*" className="hidden"
                  onChange={(e) => e.target.files?.[0] && handleFileSelect(e.target.files[0])} />
              </div>

              {/* AI Analysis result */}
              <AnimatePresence>
                {analysis && (
                  <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }}
                    className="glass rounded-2xl p-5">
                    <div className="flex items-center gap-2 mb-3">
                      <Sparkles className="w-4 h-4 text-[var(--accent-light)]" />
                      <h3 className="text-sm font-semibold">AI Food Analysis</h3>
                    </div>
                    <div className="grid grid-cols-2 gap-3 text-sm">
                      <div>
                        <p className="text-xs text-[var(--muted-foreground)]">Dish</p>
                        <p className="font-medium">{analysis.dish}</p>
                      </div>
                      <div>
                        <p className="text-xs text-[var(--muted-foreground)]">Cuisine</p>
                        <p className="font-medium">{analysis.cuisine}</p>
                      </div>
                    </div>
                    <p className="text-xs text-[var(--muted-foreground)] mt-2">{analysis.description}</p>
                    {analysis.flavor_tags && analysis.flavor_tags.length > 0 && (
                      <div className="flex flex-wrap gap-1.5 mt-3">
                        {analysis.flavor_tags.map((tag) => (
                          <span key={tag} className="px-2 py-0.5 rounded-full bg-[var(--accent)]/10 text-[var(--accent-light)] text-xs">
                            {tag}
                          </span>
                        ))}
                      </div>
                    )}
                  </motion.div>
                )}
              </AnimatePresence>

              {/* Review form */}
              <div className="glass rounded-2xl p-5 space-y-4">
                <div>
                  <label className="text-xs text-[var(--muted-foreground)] mb-1 block">Restaurant Name</label>
                  <input
                    type="text"
                    value={restaurantName}
                    onChange={(e) => setRestaurantName(e.target.value)}
                    placeholder={analysis ? `e.g. where you had ${analysis.dish}` : "Where did you eat?"}
                    className="w-full px-3 py-2 rounded-lg bg-[var(--muted)]/50 border border-[var(--border)] text-sm outline-none focus:border-[var(--accent)]/50"
                  />
                </div>

                <div>
                  <label className="text-xs text-[var(--muted-foreground)] mb-1 block">Your Review</label>
                  <textarea
                    value={reviewText}
                    onChange={(e) => setReviewText(e.target.value)}
                    placeholder="Write like you're texting a friend... e.g. 'The pad thai was incredible, perfectly spicy with fresh basil'"
                    rows={3}
                    className="w-full px-3 py-2 rounded-lg bg-[var(--muted)]/50 border border-[var(--border)] text-sm outline-none focus:border-[var(--accent)]/50 resize-none"
                  />
                </div>

                <div>
                  <label className="text-xs text-[var(--muted-foreground)] mb-1 block">Rating</label>
                  <div className="flex gap-1">
                    {[1, 2, 3, 4, 5].map((n) => (
                      <button key={n} onClick={() => setRating(n)} className="p-0.5">
                        <Star className={`w-7 h-7 transition-colors ${n <= rating ? "fill-yellow-400 text-yellow-400" : "text-[var(--muted)]"}`} />
                      </button>
                    ))}
                  </div>
                </div>

                <button
                  onClick={handleSubmit}
                  disabled={submitting || !reviewText.trim()}
                  className="w-full py-3 rounded-xl bg-[var(--accent)] text-white font-medium hover:bg-[var(--accent)]/80 disabled:opacity-40 transition-colors flex items-center justify-center gap-2"
                >
                  {submitting ? <Loader2 className="w-4 h-4 animate-spin" /> : <Sparkles className="w-4 h-4" />}
                  {submitting ? "Updating taste profile..." : "Submit & Update Profile"}
                </button>
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </div>
  );
}
